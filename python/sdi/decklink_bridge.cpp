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
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

namespace {

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
                BMDDisplayMode actual = 0;
                out->DoesSupportVideoMode(bmdVideoConnectionUnspecified, info.mode, kFormats[i],
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
    if (out->GetDisplayMode(mode, &dm) != S_OK) {
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
    HRESULT r = out->EnableVideoOutput(mode, bmdVideoOutputFlagDefault);
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
                std::memcpy(bytes, data, (size_t)o->row_bytes * o->height);   // 4K 10bit で 2.4 ms
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
    o->out->Release();
    o->out = nullptr;
    o->Release();
}
