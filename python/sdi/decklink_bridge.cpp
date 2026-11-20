// Blackmagic DeckLink / UltraStudio へ映像を出すための小さなライブラリ (Python の ctypes から呼ぶ)。
// ビルドは macOS が ../build_sdi_mac.sh (SDK のヘッダと DeckLinkAPIDispatch.cpp を使う)、
// Windows が ../build_sdi.bat (SDK の DeckLinkAPI.idl から midl で作ったヘッダと DeckLinkAPI_i.c を使う)。
// OS の違い (機器の一覧の作り方、文字列、COM の初期化) は下の「OS ごとの違い」にまとめてある。
//
// 出し方は「予約出力」(ScheduleVideoFrame)。コマの切り替えは機器が正確に行うので、絵が届くのが
// 多少遅れても出力のコマの間隔は乱れない。予約の仕方は 2 通り (zvp_dl_send の tag):
// - 出力のコマ F のために描いた絵を、F + latency コマ目に出す (停止中など)
// - 前に予約した絵のすぐ後ろへ順番に並べる (再生中で、動画と出力の fps が整数倍のとき)
// 新しい絵が来ない間 (一時停止中など) は、最後の絵を予約し続ける (hold_last)。
#ifdef _WIN32
#include <windows.h>
#include "DeckLinkAPI.h"         // midl が DeckLinkAPI.idl から作るヘッダ
#else
#include <CoreFoundation/CoreFoundation.h>
#include "DeckLinkAPI.h"
#endif

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#if defined(_M_X64) || defined(__x86_64__)
#include <smmintrin.h>           // SSE4.1 (_mm_stream_load_si128)
#define ZVP_X86 1
#endif
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

// 1 コマ分を写す。送る絵は GPU の読み出し用バッファを開いたもの (Windows の Intel GPU などでは
// CPU のキャッシュが効かないメモリ) なので、普通の memcpy だと 4K 10bit (22 MB) に 11〜18 ms かかり、
// 画面の描画まで遅らせていた。x86 ではキャッシュを通さない読み書き (SSE4.1 の streaming load と
// non-temporal store) で写す。どちらかが 16 バイト境界にないとき、x86 以外 (Apple シリコン) は memcpy
void copy_frame(void* dst, const void* src, size_t n) {
#ifdef ZVP_X86
    if (((uintptr_t)dst & 15) == 0 && ((uintptr_t)src & 15) == 0) {
        auto* d = (__m128i*)dst;
        auto* s = (__m128i*)const_cast<void*>(src);
        size_t blocks = n / 64;
        for (size_t i = 0; i < blocks; i++, d += 4, s += 4) {
            __m128i a = _mm_stream_load_si128(s), b = _mm_stream_load_si128(s + 1);
            __m128i c = _mm_stream_load_si128(s + 2), e = _mm_stream_load_si128(s + 3);
            _mm_stream_si128(d, a);
            _mm_stream_si128(d + 1, b);
            _mm_stream_si128(d + 2, c);
            _mm_stream_si128(d + 3, e);
        }
        _mm_sfence();
        size_t done = blocks * 64;
        std::memcpy((char*)dst + done, (const char*)src + done, n - done);
        return;
    }
#endif
    std::memcpy(dst, src, n);
}

// ---------- OS ごとの違い ----------
#ifdef _WIN32
#define ZVP_EXPORT extern "C" __declspec(dllexport)
#define ZVP_CALL STDMETHODCALLTYPE
typedef BSTR dl_string;
typedef BOOL dl_bool;

// DeckLink の文字列 (BSTR) を UTF-8 にして手放す
std::string to_string(BSTR s) {
    if (!s)
        return "";
    int wlen = (int)SysStringLen(s);
    int len = WideCharToMultiByte(CP_UTF8, 0, s, wlen, nullptr, 0, nullptr, nullptr);
    std::string out(len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s, wlen, &out[0], len, nullptr, nullptr);
    SysFreeString(s);
    return out;
}

// COM は呼ぶスレッドごとに初期化が要る (画面のスレッドと、送るスレッドから呼ばれる)。
// Qt の画面のスレッドはすでに STA で初期化しているので、そのときは何もしない
void com_init() {
    thread_local bool done = false;
    if (!done) {
        CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        done = true;
    }
}

IDeckLinkIterator* create_iterator() {
    IDeckLinkIterator* it = nullptr;
    if (CoCreateInstance(CLSID_CDeckLinkIterator, nullptr, CLSCTX_ALL, IID_IDeckLinkIterator, (void**)&it) != S_OK)
        return nullptr;
    return it;
}
#else
#define ZVP_EXPORT extern "C" __attribute__((visibility("default")))
#define ZVP_CALL
typedef CFStringRef dl_string;
typedef bool dl_bool;

std::string to_string(CFStringRef s) {
    if (!s)
        return "";
    char buf[512] = {};
    CFStringGetCString(s, buf, sizeof(buf), kCFStringEncodingUTF8);
    CFRelease(s);
    return buf;
}

void com_init() {}

IDeckLinkIterator* create_iterator() { return CreateDeckLinkIteratorInstance(); }
#endif
// ---------- ここまで ----------

struct Device {
    IDeckLink* deckLink;
    std::string name;
};

std::vector<Device> g_devices;
std::mutex g_lock;

void copy_out(const std::string& s, char* buf, int len) {
    if (buf && len > 0) {
        std::strncpy(buf, s.c_str(), len - 1);
        buf[len - 1] = 0;
    }
}

IDeckLinkOutput* output_of(int dev) {
    if (dev < 0 || dev >= (int)g_devices.size())
        return nullptr;
    IDeckLinkOutput* out = nullptr;
    if (g_devices[dev].deckLink->QueryInterface(IID_IDeckLinkOutput, (void**)&out) != S_OK)
        return nullptr;
    return out;
}

const BMDPixelFormat kFormats[2] = {bmdFormat8BitYUV, bmdFormat10BitYUV};

}  // namespace

struct ModeInfo {
    uint32_t mode;          // BMDDisplayMode
    char name[64];
    int32_t width, height;
    int64_t frame_duration, time_scale;   // fps = time_scale / frame_duration
    int32_t interlaced;
    int32_t ok8, ok10;      // 8bit YUV (2vuy) / 10bit YUV (v210) で出せるか
};

// 描いてから出すまでのコマ数。絵が届くのが 2 コマ近く遅れても間に合う。4K など 1 コマが大きい形式は
// 機器のコマへ写すのに 1 コマ以上かかることがあるので 1 コマ増やす
const int kLatency = 3;
const int kLatencyLarge = 4;
const size_t kLargeFrame = 8u << 20;
const int64_t kSequence = -2;            // zvp_dl_send の tag: 前の予約の後ろへ順番に並べる
const int kPoolSize = 10;    // 出力用のコマの数 (予約中のものは書き換えない)

struct Slot {
    IDeckLinkMutableVideoFrame* frame;
    int in_use;              // 予約されてまだ出し終わっていない回数 (書き込み中も 1 とする)
};

class Output : public IDeckLinkVideoOutputCallback {
public:
    IDeckLinkOutput* out = nullptr;
    std::vector<Slot> pool;
    int32_t row_bytes = 0, height = 0;
    int latency = kLatency;
    BMDTimeValue duration = 0;
    BMDTimeScale scale = 0;
    std::mutex lock;
    bool closed = false;         // 閉じ始めた (コールバックは何もしない。zvp_dl_close が lock を持って立てる)
    int last = -1;               // 最後に予約した絵 (pool の番号)
    int64_t last_slot = -1;      // 最後に予約した出力のコマ番号
    int64_t holds = 0;           // 新しい絵が間に合わず、最後の絵を繰り返して予約したコマ数
    int64_t resyncs = 0;         // 順番に並べていて、予約が尽きかけて並べ直した回数
    std::atomic<int> refs{1};

    // いま出している出力のコマ番号とその中の位置。動いていなければ false
    bool now(int64_t* frame, double* phase) {
        BMDTimeValue t = 0;
        double speed = 0;
        if (out->GetScheduledStreamTime(scale, &t, &speed) != S_OK || duration <= 0)
            return false;
        *frame = t / duration;
        if (phase)
            *phase = (double)(t % duration) / (double)duration;
        return true;
    }

    // slot に pool[i] を予約する (lock を持って呼ぶ)
    bool schedule(int i, int64_t slot) {
        if (out->ScheduleVideoFrame(pool[i].frame, slot * duration, duration, scale) != S_OK)
            return false;
        pool[i].in_use++;
        last = i;
        last_slot = slot;
        return true;
    }

    // 新しい絵が来ていなければ、最後の絵を少し先まで予約しておく (lock を持って呼ぶ)
    void hold_last() {
        int64_t frame = 0;
        if (last < 0 || !now(&frame, nullptr))
            return;
        if (last_slot < frame)
            last_slot = frame;
        while (last_slot < frame + 2) {
            if (!schedule(last, last_slot + 1))
                break;
            holds++;
        }
    }

    HRESULT ZVP_CALL ScheduledFrameCompleted(IDeckLinkVideoFrame* done, BMDOutputFrameCompletionResult) override {
        std::lock_guard<std::mutex> g(lock);
        // 閉じている途中で届いた知らせ: SDK は止めたあとも、すでに呼び始めた知らせを終わらせずに戻ることがある。
        // pool や out はもう手放しているかもしれないので触らない
        if (closed)
            return S_OK;
        for (auto& s : pool)
            if (s.frame == done && s.in_use > 0) {
                s.in_use--;
                break;
            }
        hold_last();
        return S_OK;
    }
    HRESULT ZVP_CALL ScheduledPlaybackHasStopped() override { return S_OK; }
    HRESULT ZVP_CALL QueryInterface(REFIID, void** ppv) override {
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG ZVP_CALL AddRef() override { return ++refs; }
    ULONG ZVP_CALL Release() override {
        ULONG r = --refs;
        if (r == 0)
            delete this;
        return r;
    }
};

// 機器を探し直す。映像を出せる機器の数を返す (ドライバが無ければ -1)。
ZVP_EXPORT int zvp_dl_refresh() {
    com_init();
    std::lock_guard<std::mutex> g(g_lock);
    for (auto& d : g_devices)
        d.deckLink->Release();
    g_devices.clear();
    IDeckLinkIterator* it = create_iterator();
    if (!it)
        return -1;
    IDeckLink* d = nullptr;
    while (it->Next(&d) == S_OK) {
        IDeckLinkOutput* out = nullptr;
        if (d->QueryInterface(IID_IDeckLinkOutput, (void**)&out) == S_OK) {
            out->Release();
            dl_string name = nullptr;
            d->GetDisplayName(&name);
            g_devices.push_back({d, to_string(name)});
        } else {
            d->Release();
        }
    }
    it->Release();
    return (int)g_devices.size();
}

ZVP_EXPORT int zvp_dl_device_name(int dev, char* buf, int len) {
    std::lock_guard<std::mutex> g(g_lock);
    if (dev < 0 || dev >= (int)g_devices.size())
        return -1;
    copy_out(g_devices[dev].name, buf, len);
    return 0;
}

// 出力形式を max 個まで modes に書き、書いた数を返す。
ZVP_EXPORT int zvp_dl_modes(int dev, ModeInfo* modes, int max) {
    com_init();
    std::lock_guard<std::mutex> g(g_lock);
    IDeckLinkOutput* out = output_of(dev);
    if (!out)
        return -1;
    IDeckLinkDisplayModeIterator* it = nullptr;
    int n = 0;
    if (out->GetDisplayModeIterator(&it) == S_OK) {
        IDeckLinkDisplayMode* m = nullptr;
        while (n < max && it->Next(&m) == S_OK) {
            ModeInfo& info = modes[n];
            std::memset(&info, 0, sizeof(info));
            info.mode = m->GetDisplayMode();
            dl_string name = nullptr;
            m->GetName(&name);
            copy_out(to_string(name), info.name, sizeof(info.name));
            info.width = (int32_t)m->GetWidth();
            info.height = (int32_t)m->GetHeight();
            BMDTimeValue dur = 0;
            BMDTimeScale scale = 0;
            m->GetFrameRate(&dur, &scale);
            info.frame_duration = dur;
            info.time_scale = scale;
            info.interlaced = m->GetFieldDominance() == bmdLowerFieldFirst ||
                              m->GetFieldDominance() == bmdUpperFieldFirst;
            int32_t* ok[2] = {&info.ok8, &info.ok10};
            for (int i = 0; i < 2; i++) {
                dl_bool supported = false;
                // Windows の SDK では BMDDisplayMode が列挙型なので、数値から明示的に変換する
                BMDDisplayMode actual = static_cast<BMDDisplayMode>(0);
                out->DoesSupportVideoMode(bmdVideoConnectionUnspecified, static_cast<BMDDisplayMode>(info.mode), kFormats[i],
                                          bmdNoVideoOutputConversion, bmdSupportedVideoModeDefault,
                                          &actual, &supported);
                *ok[i] = supported ? 1 : 0;
            }
            m->Release();
            if (info.ok8 || info.ok10)
                n++;
        }
        it->Release();
    }
    out->Release();
    return n;
}

// 出力を始める。失敗したら nullptr を返し、err に理由を書く。
ZVP_EXPORT Output* zvp_dl_open(int dev, uint32_t mode, int ten_bit, char* err, int err_len) {
    com_init();
    std::lock_guard<std::mutex> g(g_lock);
    IDeckLinkOutput* out = output_of(dev);
    if (!out) {
        copy_out("機器が見つかりません", err, err_len);
        return nullptr;
    }
    IDeckLinkDisplayMode* dm = nullptr;
    if (out->GetDisplayMode(static_cast<BMDDisplayMode>(mode), &dm) != S_OK) {
        out->Release();
        copy_out("この出力形式は使えません", err, err_len);
        return nullptr;
    }
    int32_t w = (int32_t)dm->GetWidth(), h = (int32_t)dm->GetHeight();
    auto* o = new Output();
    dm->GetFrameRate(&o->duration, &o->scale);
    dm->Release();
    o->out = out;
    o->height = h;
    BMDPixelFormat fmt = kFormats[ten_bit ? 1 : 0];
    HRESULT r = out->EnableVideoOutput(static_cast<BMDDisplayMode>(mode), bmdVideoOutputFlagDefault);
    if (r != S_OK) {
        copy_out(r == E_ACCESSDENIED ? "ほかのアプリが出力を使っています" : "出力を始められませんでした",
                 err, err_len);
        out->Release();
        o->out = nullptr;
        o->Release();
        return nullptr;
    }
    int row_bytes = 0;
    out->RowBytesForPixelFormat(fmt, w, &row_bytes);
    o->row_bytes = row_bytes;
    if ((size_t)o->row_bytes * h > kLargeFrame)
        o->latency = kLatencyLarge;
    for (int i = 0; i < kPoolSize; i++) {
        IDeckLinkMutableVideoFrame* f = nullptr;
        if (out->CreateVideoFrame(w, h, o->row_bytes, fmt, bmdFrameFlagDefault, &f) != S_OK)
            break;
        o->pool.push_back({f, 0});
    }
    if ((int)o->pool.size() < kPoolSize || out->SetScheduledFrameCompletionCallback(o) != S_OK ||
        out->StartScheduledPlayback(0, o->scale, 1.0) != S_OK) {
        copy_out("出力を始められませんでした", err, err_len);
        out->SetScheduledFrameCompletionCallback(nullptr);
        out->DisableVideoOutput();
        for (auto& s : o->pool)
            s.frame->Release();
        out->Release();
        o->out = nullptr;
        o->Release();
        return nullptr;
    }
    return o;
}

ZVP_EXPORT int zvp_dl_row_bytes(Output* o) { return o ? o->row_bytes : 0; }

// 出力のクロック: いま出しているコマの番号 (frame) と、そのコマの中の位置 (0〜1)。成功なら 0。
ZVP_EXPORT int zvp_dl_clock(Output* o, int64_t* frame, double* phase) {
    com_init();
    if (!o)
        return -1;
    return o->now(frame, phase) ? 0 : -2;
}

// 最後の絵を繰り返して予約したコマ数と、並べ直した回数 (確認用)。
ZVP_EXPORT int zvp_dl_stats(Output* o, int64_t* holds, int64_t* resyncs) {
    if (!o)
        return -1;
    std::lock_guard<std::mutex> g(o->lock);
    *holds = o->holds;
    *resyncs = o->resyncs;
    return 0;
}

// 予約されていて、まだ出していないコマの数 (確認用)。
ZVP_EXPORT int zvp_dl_buffered(Output* o) {
    com_init();
    unsigned int n = 0;
    if (!o || o->out->GetBufferedVideoFrameCount(&n) != S_OK)
        return -1;
    return (int)n;
}

// 1 コマ分 (row_bytes * height バイト、上の行から) を送る。tag の意味:
//   0 以上   出力のコマ tag のために描いた絵。tag + latency (3 か 4) コマ目に出す
//   -1       いまのコマのために描いた絵 (tag = いまのコマ)
//   -2       順番に並べる (kSequence): 前に予約した絵のすぐ後ろへ repeat コマ分続けて予約する。
//            動画と出力の fps が整数倍のときに使う。機器のほうが速くて足りなくなったら、
//            最後の絵が続く (hold_last)。画面のほうが速くて予約が先へ行き過ぎたら 1 枚捨てて合わせる
// 戻り値: 0 予約した、1 間に合わず捨てた、2 先へ行き過ぎて捨てた、3 空いているコマが無い、負 失敗。
ZVP_EXPORT int zvp_dl_send(Output* o, const void* data, int64_t tag, int repeat) {
    com_init();
    if (!o)
        return -1;
    int index = -1;
    {
        std::lock_guard<std::mutex> g(o->lock);
        int64_t frame = 0;
        if (!o->now(&frame, nullptr))
            return -2;
        if (tag == -1)
            tag = frame;
        if (tag >= 0 && tag + o->latency <= frame)
            return 1;
        for (int i = 0; i < (int)o->pool.size(); i++)
            if (o->pool[i].in_use == 0 && i != o->last) {
                index = i;
                break;
            }
        if (index < 0)
            return 3;
        o->pool[index].in_use = 1;           // 書き込み中 (ほかで使わない)
    }
    IDeckLinkMutableVideoFrame* f = o->pool[index].frame;
    int result = -3;
    IDeckLinkVideoBuffer* buf = nullptr;
    if (f->QueryInterface(IID_IDeckLinkVideoBuffer, (void**)&buf) == S_OK) {
        void* bytes = nullptr;
        if (buf->StartAccess(bmdBufferAccessWrite) == S_OK) {
            if (buf->GetBytes(&bytes) == S_OK) {
                copy_frame(bytes, data, (size_t)o->row_bytes * o->height);
                result = 0;
            }
            buf->EndAccess(bmdBufferAccessWrite);
        }
        buf->Release();
    }
    std::lock_guard<std::mutex> g(o->lock);
    o->pool[index].in_use = 0;
    if (result != 0)
        return result;
    int64_t frame = 0;
    o->now(&frame, nullptr);
    if (tag == kSequence) {
        int64_t slot = o->last_slot + 1;
        if (slot < frame + 2) {              // 予約が尽きかけている: 少し先から並べ直す (間は前の絵が続く)
            slot = frame + o->latency;
            o->resyncs++;
        }
        if (slot > frame + o->latency + 1)   // 予約が先へ行き過ぎた: この絵は出さずに合わせる
            return 2;
        for (int k = 0; k < (repeat > 0 ? repeat : 1); k++)
            if (!o->schedule(index, slot + k))
                return -4;
        return 0;
    }
    int64_t slot = tag + o->latency;
    if (slot <= o->last_slot)            // 前の絵 (か、止まっている間の繰り返し) の後ろへ
        slot = o->last_slot + 1;
    if (slot <= frame)
        return 1;
    if (slot > frame + o->latency + 2)
        return 2;
    return o->schedule(index, slot) ? 0 : -4;
}

ZVP_EXPORT void zvp_dl_close(Output* o) {
    com_init();
    if (!o)
        return;
    {
        std::lock_guard<std::mutex> g(o->lock);
        o->closed = true;            // これより後に届いた完了の知らせは何もしない
        o->last = -1;
    }
    BMDTimeValue stopped = 0;
    o->out->StopScheduledPlayback(0, &stopped, 0);
    o->out->SetScheduledFrameCompletionCallback(nullptr);
    o->out->DisableVideoOutput();
    {
        std::lock_guard<std::mutex> g(o->lock);
        for (auto& s : o->pool)
            s.frame->Release();
        o->pool.clear();
    }
    o->out->Release();               // out は残しておく (o が消えるまで、ほかから読まれても止まらないように)
    o->Release();
}

// ======================================================================================
// 入力 (SDI などの映像を受け取る)
//
// 入力の形式 (解像度・fps) は機器に見分けさせる (format detection)。届いたコマは 10bit YUV 4:2:2 (v210) で
// 受け取り、P210 (Y のプレーンと、Cb Cr を交互に並べたプレーン。10bit を 16bit の上位に詰める) にほどいて、
// Python が用意したバッファへ書く。P210 は画面 (GPU) がそのまま読め、録画 (HEVC) にもそのまま渡せる。
// バッファは何枚かを順に使い、画面が表示中の 1 枚 (held) と最新の 1 枚 (latest) には書かない。
// ======================================================================================

namespace {

std::vector<IDeckLink*> g_inputs;

// v210 の 6 画素 (4 語) → P210 の Y 6 つと CbCr 3 組。v210 の並び:
//   語 0: Cb0 Y0 Cr0 / 語 1: Y1 Cb1 Y2 / 語 2: Cr1 Y3 Cb2 / 語 3: Y4 Cr2 Y5 (各語の下位から 10bit ずつ)
// P210 は 10bit を 16bit の上位に詰めるので、各値を 6 ビット左へずらす。取り出しとずらしをまとめて、
// 「下位 10bit を 6 ビット上げる」は (w << 6)、「11〜20 ビット目」は ((w >> 4) & 0xFFC0)、
// 「21〜30 ビット目」は ((w >> 14) & 0xFFC0) の 1 回で済ませる
inline void unpack_v210_group(const uint32_t* s, uint16_t* y, uint16_t* uv) {
    uint32_t w0 = s[0], w1 = s[1], w2 = s[2], w3 = s[3];
    uv[0] = (uint16_t)(w0 << 6);
    y[0] = (uint16_t)((w0 >> 4) & 0xFFC0);
    uv[1] = (uint16_t)((w0 >> 14) & 0xFFC0);
    y[1] = (uint16_t)(w1 << 6);
    uv[2] = (uint16_t)((w1 >> 4) & 0xFFC0);
    y[2] = (uint16_t)((w1 >> 14) & 0xFFC0);
    uv[3] = (uint16_t)(w2 << 6);
    y[3] = (uint16_t)((w2 >> 4) & 0xFFC0);
    uv[4] = (uint16_t)((w2 >> 14) & 0xFFC0);
    y[4] = (uint16_t)(w3 << 6);
    uv[5] = (uint16_t)((w3 >> 4) & 0xFFC0);
    y[5] = (uint16_t)((w3 >> 14) & 0xFFC0);
}

// v210 の 1 行 → P210 の Y の行と CbCr の行 (1080p で 0.7 ms、4K で 2.8 ms)。
// 幅が 6 の倍数でないとき (1280 など) は、最後のかたまりだけ一時の場所にほどいて要る分を写す
void unpack_v210_row(const uint32_t* src, uint16_t* y, uint16_t* uv, int width) {
    int full = width / 6 * 6, x = 0;
    for (; x < full; x += 6, src += 4)
        unpack_v210_group(src, y + x, uv + x);
    if (x < width) {
        uint16_t ty[6], tuv[6];
        unpack_v210_group(src, ty, tuv);
        int n = width - x;
        std::memcpy(y + x, ty, n * sizeof(uint16_t));
        std::memcpy(uv + x, tuv, n * sizeof(uint16_t));      // CbCr の行も幅と同じ数 (奇数の幅ではみ出さない)
    }
}

}  // namespace

class InputCapture : public IDeckLinkInputCallback {
public:
    IDeckLinkInput* in = nullptr;
    std::mutex lock;
    int32_t width = 0, height = 0;
    BMDTimeValue duration = 0;
    BMDTimeScale scale = 0;
    std::string mode_name, error;
    int64_t serial = 0;              // 入力の形式が変わった回数
    bool signal = false;             // 入力に信号があるか
    int64_t frames = 0, dropped = 0; // 届いたコマ / 書けずに捨てたコマ
    std::vector<uint8_t*> bufs;      // Python が用意した P210 のバッファ
    int32_t buf_w = 0, buf_h = 0;
    int held = -1, latest = -1, writing = -1;
    bool closing = false;            // 閉じ始めた (コールバックは何もしない)
    int busy = 0;                    // 動いているコールバックの数 (閉じるときは 0 になるのを待つ)
    std::condition_variable idle;    // writing が -1 になった・busy が減ったときに知らせる
    int64_t latest_no = 0;
    double latest_t = 0;
    std::atomic<int> refs{1};

    void set_mode(IDeckLinkDisplayMode* m) {
        width = (int32_t)m->GetWidth();
        height = (int32_t)m->GetHeight();
        m->GetFrameRate(&duration, &scale);
        dl_string name = nullptr;
        m->GetName(&name);
        mode_name = to_string(name);
    }

    // コールバックの始めと終わり。閉じ始めていたら false (何もしないで戻る)
    bool enter() {
        std::lock_guard<std::mutex> g(lock);
        if (closing)
            return false;
        busy++;
        return true;
    }
    void leave() {
        {
            std::lock_guard<std::mutex> g(lock);
            busy--;
        }
        idle.notify_all();
    }
    struct Busy {
        InputCapture* c;
        bool ok;
        explicit Busy(InputCapture* c) : c(c), ok(c->enter()) {}
        ~Busy() {
            if (ok)
                c->leave();
        }
    };

    HRESULT ZVP_CALL VideoInputFormatChanged(BMDVideoInputFormatChangedEvents, IDeckLinkDisplayMode* m,
                                             BMDDetectedVideoInputFormatFlags flags) override {
        Busy busy_guard(this);
        if (!busy_guard.ok)          // 閉じている途中で入力を始め直さない
            return S_OK;
        com_init();
        in->PauseStreams();
        in->EnableVideoInput(m->GetDisplayMode(), bmdFormat10BitYUV, bmdVideoInputEnableFormatDetection);
        in->FlushStreams();
        in->StartStreams();
        std::lock_guard<std::mutex> g(lock);
        set_mode(m);
        error = (flags & bmdDetectedVideoInputRGB444) ? "RGB の入力には対応していません" : "";
        latest = -1;
        serial++;
        return S_OK;
    }

    HRESULT ZVP_CALL VideoInputFrameArrived(IDeckLinkVideoInputFrame* f, IDeckLinkAudioInputPacket*) override {
        Busy busy_guard(this);
        if (!f || !busy_guard.ok)    // 閉じている途中: Python のバッファはもう無いかもしれない
            return S_OK;
        int idx = -1;
        int32_t w = (int32_t)f->GetWidth(), h = (int32_t)f->GetHeight();
        {
            std::lock_guard<std::mutex> g(lock);
            signal = !(f->GetFlags() & bmdFrameHasNoInputSource);
            if (!signal)
                return S_OK;
            frames++;
            if (bufs.empty() || buf_w != w || buf_h != h || f->GetPixelFormat() != bmdFormat10BitYUV) {
                dropped++;
                return S_OK;
            }
            for (int i = 0; i < (int)bufs.size(); i++)
                if (i != held && i != latest) {
                    idx = i;
                    break;
                }
            if (idx < 0) {
                dropped++;
                return S_OK;
            }
            writing = idx;
        }
        // ほどくのは lock の外で行う (4K では数 ms かかり、その間も画面側が最新のコマを取れるように)
        IDeckLinkVideoBuffer* vb = nullptr;
        bool ok = false;
        if (f->QueryInterface(IID_IDeckLinkVideoBuffer, (void**)&vb) == S_OK) {
            void* bytes = nullptr;
            if (vb->StartAccess(bmdBufferAccessRead) == S_OK) {
                if (vb->GetBytes(&bytes) == S_OK) {
                    long row_bytes = f->GetRowBytes();
                    uint16_t* y = (uint16_t*)bufs[idx];
                    uint16_t* uv = y + (size_t)w * h;
                    for (int r = 0; r < h; r++)
                        unpack_v210_row((const uint32_t*)((const uint8_t*)bytes + (size_t)row_bytes * r),
                                        y + (size_t)w * r, uv + (size_t)w * r, w);
                    ok = true;
                }
                vb->EndAccess(bmdBufferAccessRead);
            }
            vb->Release();
        }
        BMDTimeValue t = 0, d = 0;
        f->GetStreamTime(&t, &d, 1000000);
        std::lock_guard<std::mutex> g(lock);
        writing = -1;
        idle.notify_all();
        if (!ok) {
            dropped++;
            return S_OK;
        }
        latest = idx;
        latest_no = frames;
        latest_t = (double)t / 1000000.0;
        return S_OK;
    }

    HRESULT ZVP_CALL QueryInterface(REFIID, void** ppv) override {
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG ZVP_CALL AddRef() override { return ++refs; }
    ULONG ZVP_CALL Release() override {
        ULONG r = --refs;
        if (r == 0)
            delete this;
        return r;
    }
};

// 入力のある機器を探し直す。数を返す (ドライバが無ければ -1)。
ZVP_EXPORT int zvp_in_refresh() {
    com_init();
    std::lock_guard<std::mutex> g(g_lock);
    for (auto* d : g_inputs)
        d->Release();
    g_inputs.clear();
    IDeckLinkIterator* it = create_iterator();
    if (!it)
        return -1;
    IDeckLink* d = nullptr;
    while (it->Next(&d) == S_OK) {
        IDeckLinkInput* in = nullptr;
        if (d->QueryInterface(IID_IDeckLinkInput, (void**)&in) == S_OK) {
            in->Release();
            g_inputs.push_back(d);
        } else {
            d->Release();
        }
    }
    it->Release();
    return (int)g_inputs.size();
}

ZVP_EXPORT int zvp_in_device_name(int dev, char* buf, int len) {
    com_init();
    std::lock_guard<std::mutex> g(g_lock);
    if (dev < 0 || dev >= (int)g_inputs.size())
        return -1;
    dl_string name = nullptr;
    g_inputs[dev]->GetDisplayName(&name);
    copy_out(to_string(name), buf, len);
    return 0;
}

// 入力を始める。初めは 1080p59.94 で待ち、信号が来たら機器が見分けた形式に切り替える。
ZVP_EXPORT InputCapture* zvp_in_open(int dev, char* err, int err_len) {
    com_init();
    std::lock_guard<std::mutex> g(g_lock);
    if (dev < 0 || dev >= (int)g_inputs.size()) {
        copy_out("機器が見つかりません", err, err_len);
        return nullptr;
    }
    auto* c = new InputCapture();
    if (g_inputs[dev]->QueryInterface(IID_IDeckLinkInput, (void**)&c->in) != S_OK) {
        copy_out("入力を使えません", err, err_len);
        c->Release();
        return nullptr;
    }
    IDeckLinkDisplayMode* m = nullptr;
    if (c->in->GetDisplayMode(bmdModeHD1080p5994, &m) == S_OK) {
        c->set_mode(m);
        m->Release();
    }
    HRESULT r = c->in->EnableVideoInput(bmdModeHD1080p5994, bmdFormat10BitYUV, bmdVideoInputEnableFormatDetection);
    if (r != S_OK || c->in->SetCallback(c) != S_OK || c->in->StartStreams() != S_OK) {
        copy_out(r == E_ACCESSDENIED ? "ほかのアプリが入力を使っています" : "入力を始められませんでした", err, err_len);
        c->in->SetCallback(nullptr);
        c->in->DisableVideoInput();
        c->in->Release();
        c->in = nullptr;
        c->Release();
        return nullptr;
    }
    return c;
}

// 入力の形式。信号の有無、形式が変わった回数 (変わったらバッファを用意し直す)、問題があればその説明。
ZVP_EXPORT int zvp_in_format(InputCapture* c, int32_t* w, int32_t* h, int64_t* duration, int64_t* scale,
                             char* name, int name_len, int64_t* serial, int32_t* signal, char* err, int err_len) {
    if (!c)
        return -1;
    std::lock_guard<std::mutex> g(c->lock);
    *w = c->width;
    *h = c->height;
    *duration = c->duration;
    *scale = c->scale;
    copy_out(c->mode_name, name, name_len);
    *serial = c->serial;
    *signal = c->signal ? 1 : 0;
    copy_out(c->error, err, err_len);
    return 0;
}

// P210 のバッファ (各 width * height * 4 バイト) を渡す。書き込み中のコマがあれば書き終わるのを待つ。
ZVP_EXPORT int zvp_in_set_buffers(InputCapture* c, void** bufs, int n, int32_t w, int32_t h) {
    if (!c)
        return -1;
    std::unique_lock<std::mutex> g(c->lock);
    c->idle.wait(g, [c] { return c->writing < 0; });
    c->bufs.assign((uint8_t**)bufs, (uint8_t**)bufs + n);
    c->buf_w = w;
    c->buf_h = h;
    c->held = c->latest = -1;
    return 0;
}

// after より新しいコマがあれば、そのバッファの番号を返して表示中 (held) にする (前の held は手放す)。
// 無ければ -1。no にコマの通し番号、t に入力の時刻 (秒)。
ZVP_EXPORT int zvp_in_latest(InputCapture* c, int64_t after, int64_t* no, double* t) {
    if (!c)
        return -1;
    std::lock_guard<std::mutex> g(c->lock);
    if (c->latest < 0 || c->latest_no <= after)
        return -1;
    c->held = c->latest;
    *no = c->latest_no;
    *t = c->latest_t;
    return c->held;
}

// 表示中のバッファを手放す (入力を切り替えるときなど)。
ZVP_EXPORT void zvp_in_release(InputCapture* c) {
    if (!c)
        return;
    std::lock_guard<std::mutex> g(c->lock);
    c->held = -1;
}

ZVP_EXPORT int zvp_in_stats(InputCapture* c, int64_t* frames, int64_t* dropped) {
    if (!c)
        return -1;
    std::lock_guard<std::mutex> g(c->lock);
    *frames = c->frames;
    *dropped = c->dropped;
    return 0;
}

ZVP_EXPORT void zvp_in_close(InputCapture* c) {
    com_init();
    if (!c)
        return;
    {
        // 新しいコールバックを止め、動いているもの (コマをほどいている途中など) が終わるのを待つ。
        // そのあとは Python のバッファに書かないので、Python が閉じたあとに消しても安全
        std::unique_lock<std::mutex> g(c->lock);
        c->closing = true;
        c->idle.wait(g, [c] { return c->busy == 0; });
        c->bufs.clear();
    }
    c->in->StopStreams();
    c->in->SetCallback(nullptr);
    c->in->DisableVideoInput();
    c->in->Release();                // in は残しておく (c が消えるまで、ほかから読まれても止まらないように)
    c->Release();
}

// v210 のコマ (row_bytes ずつの行) を P210 (Y の後に CbCr) にほどく。確かめるとき用 (入力では使わない)。
ZVP_EXPORT void zvp_unpack_v210(const void* src, int64_t row_bytes, void* dst, int32_t width, int32_t height) {
    uint16_t* y = (uint16_t*)dst;
    uint16_t* uv = y + (size_t)width * height;
    for (int r = 0; r < height; r++)
        unpack_v210_row((const uint32_t*)((const uint8_t*)src + row_bytes * r), y + (size_t)width * r,
                        uv + (size_t)width * r, width);
}
