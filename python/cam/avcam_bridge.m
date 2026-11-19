// macOS のカメラを AVFoundation で直接読むライブラリ (python/player_capture.py が ctypes で使う)。
//
// FFmpeg (avfoundation) は、同じ大きさの形式が 2 つ (MJPEG を展開した 420v と、圧縮しない yuvs など) あると
// 一覧の後ろのものを選び、形式の種類も指定できない。USB カメラによっては後ろの形式ではコマが届かず
// (4K U3 Camera の 4K は yuvs が後ろで、yuvs では 1 コマも来ない)、開くところで止まる。
// ここでは大きさと fps が合う形式のうち 420v / 420f (NV12) を優先して選び、NV12 で受け取る。
//
// コマは AVFoundation のスレッドで届く。最新の 1 枚だけを持っておき、Python は新しいコマが届くまで待って写す。
#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#include <stdint.h>
#include <string.h>

#define EXPORT __attribute__((visibility("default")))

static void set_err(char *err, int len, NSString *text) {
    if (err && len > 0) {
        strncpy(err, text.UTF8String ?: "", (size_t)len - 1);
        err[len - 1] = 0;
    }
}

static FourCharCode subtype(AVCaptureDeviceFormat *f) {
    return CMFormatDescriptionGetMediaSubType(f.formatDescription);
}

// 映像のカメラすべて (つないだ機器・内蔵カメラ・iPhone のカメラ)
static NSArray<AVCaptureDevice *> *all_cameras(void) {
    if (@available(macOS 14.0, *)) {
        return [AVCaptureDeviceDiscoverySession
                   discoverySessionWithDeviceTypes:@[ AVCaptureDeviceTypeExternal, AVCaptureDeviceTypeBuiltInWideAngleCamera,
                                                      AVCaptureDeviceTypeContinuityCamera, AVCaptureDeviceTypeDeskViewCamera ]
                                         mediaType:AVMediaTypeVideo
                                          position:AVCaptureDevicePositionUnspecified].devices;
    }
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    return [AVCaptureDevice devicesWithMediaType:AVMediaTypeVideo];
#pragma clang diagnostic pop
}

// 名前が一致するカメラ。FFmpeg の一覧の名前は前後の空白が落ちている (「4K U3 Camera 」が「4K U3 Camera」になる)
// のでそろえて比べる
static AVCaptureDevice *find_camera(const char *name) {
    NSCharacterSet *space = [NSCharacterSet whitespaceAndNewlineCharacterSet];
    NSString *want = [[NSString stringWithUTF8String:name ?: ""] stringByTrimmingCharactersInSet:space];
    for (AVCaptureDevice *d in all_cameras()) {
        if ([[d.localizedName stringByTrimmingCharactersInSet:space] isEqualToString:want]) return d;
    }
    return nil;
}

@interface ZvpCam : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>
@property(nonatomic, strong) AVCaptureSession *session;
@property(nonatomic, strong) AVCaptureDevice *device;
@property(nonatomic, strong) dispatch_queue_t queue;
@property(nonatomic) double fps;
@end

@implementation ZvpCam {
    CVPixelBufferRef _latest;       // 最新のコマ (_cond で守る)
    int64_t _seq;                   // 届いたコマの番号 (1 から)
    double _time;                   // 最新のコマの時刻 (秒)
    NSCondition *_cond;             // 新しいコマが届いたら知らせる
}

- (instancetype)init {
    if ((self = [super init])) {
        _cond = [NSCondition new];
    }
    return self;
}

- (void)dealloc {
    if (_latest) CVPixelBufferRelease(_latest);
}

- (void)captureOutput:(AVCaptureOutput *)output didOutputSampleBuffer:(CMSampleBufferRef)sb
       fromConnection:(AVCaptureConnection *)connection {
    CVPixelBufferRef px = CMSampleBufferGetImageBuffer(sb);
    if (!px) return;
    CMTime t = CMSampleBufferGetPresentationTimeStamp(sb);
    CVPixelBufferRetain(px);
    [_cond lock];
    if (_latest) CVPixelBufferRelease(_latest);
    _latest = px;
    _seq += 1;
    _time = CMTIME_IS_VALID(t) ? CMTimeGetSeconds(t) : 0.0;
    [_cond broadcast];
    [_cond unlock];
}

// last より新しいコマが届くまで、最長 timeout 秒待つ。新しいコマの番号 (大きさと時刻も)、届かなければ 0、
// カメラが外れた・止まったら -1 (Python が開き直す)
- (int64_t)waitAfter:(int64_t)last timeout:(double)timeout width:(int *)w height:(int *)h time:(double *)time {
    NSDate *until = [NSDate dateWithTimeIntervalSinceNow:timeout];
    [_cond lock];
    while (_seq <= last && [_cond waitUntilDate:until]) {
    }
    int64_t seq = _seq > last && _latest ? _seq : 0;
    if (seq) {
        *w = (int)CVPixelBufferGetWidth(_latest);
        *h = (int)CVPixelBufferGetHeight(_latest);
        *time = _time;
    }
    [_cond unlock];
    if (!seq && (!self.device.isConnected || !self.session.isRunning)) return -1;
    return seq;
}

// 最新のコマを NV12 (Y を w×h、続けて CbCr を w×h/2。行の詰め物なし) で dst へ写す。写したコマの番号を返す
- (int64_t)copyTo:(uint8_t *)dst size:(int64_t)size width:(int)w height:(int)h {
    [_cond lock];
    CVPixelBufferRef px = _latest ? CVPixelBufferRetain(_latest) : NULL;
    int64_t seq = _seq;
    [_cond unlock];
    if (!px) return 0;
    int64_t result = -1;
    if ((int)CVPixelBufferGetWidth(px) >= w && (int)CVPixelBufferGetHeight(px) >= h &&
        CVPixelBufferGetPlaneCount(px) == 2 && size >= (int64_t)w * h * 3 / 2 &&
        CVPixelBufferLockBaseAddress(px, kCVPixelBufferLock_ReadOnly) == kCVReturnSuccess) {
        int rows[2] = {h, h / 2};
        uint8_t *out = dst;
        for (int p = 0; p < 2; p++) {
            const uint8_t *src = CVPixelBufferGetBaseAddressOfPlane(px, p);
            size_t stride = CVPixelBufferGetBytesPerRowOfPlane(px, p);
            if (stride == (size_t)w) {
                memcpy(out, src, (size_t)w * rows[p]);
            } else {
                for (int y = 0; y < rows[p]; y++) memcpy(out + (size_t)y * w, src + y * stride, (size_t)w);
            }
            out += (size_t)w * rows[p];
        }
        CVPixelBufferUnlockBaseAddress(px, kCVPixelBufferLock_ReadOnly);
        result = seq;
    }
    CVPixelBufferRelease(px);
    return result;
}
@end

// 名前 (macOS が見せるカメラの名前) が一致するカメラを、width×height・fps の形式で開く。
// fps は形式の fps の範囲 (このカメラでは 60.00024 などの決まった値) に 0.01 以内で合えばよい。
// 失敗したら NULL を返し、err に理由を書く
EXPORT void *zvp_cam_open(const char *name, int width, int height, double fps, char *err, int errlen) {
    @autoreleasepool {
        AVCaptureDevice *dev = find_camera(name);
        if (!dev) {
            set_err(err, errlen, [NSString stringWithFormat:@"カメラが見つかりません: %s", name ?: ""]);
            return NULL;
        }
        // 大きさと fps が合う形式。NV12 (420v / 420f) を優先する
        AVCaptureDeviceFormat *chosen = nil;
        AVFrameRateRange *range = nil;
        for (AVCaptureDeviceFormat *f in dev.formats) {
            CMVideoDimensions dim = CMVideoFormatDescriptionGetDimensions(f.formatDescription);
            if (dim.width != width || dim.height != height) continue;
            AVFrameRateRange *match = nil;
            for (AVFrameRateRange *r in f.videoSupportedFrameRateRanges) {
                if (fps >= r.minFrameRate - 0.01 && fps <= r.maxFrameRate + 0.01) { match = r; break; }
            }
            if (!match) continue;
            FourCharCode st = subtype(f);
            BOOL nv12 = st == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ||
                        st == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange;
            if (!chosen || nv12) {
                chosen = f;
                range = match;
                if (nv12) break;
            }
        }
        if (!chosen) {
            set_err(err, errlen, [NSString stringWithFormat:@"%d×%d %.2ffps の形式がありません", width, height, fps]);
            return NULL;
        }
        NSError *e = nil;
        AVCaptureDeviceInput *input = [AVCaptureDeviceInput deviceInputWithDevice:dev error:&e];
        if (!input) {
            set_err(err, errlen, e.localizedDescription ?: @"カメラを開けません");
            return NULL;
        }
        ZvpCam *cam = [ZvpCam new];
        cam.device = dev;
        cam.queue = dispatch_queue_create("zvp.camera", DISPATCH_QUEUE_SERIAL);
        AVCaptureSession *s = [AVCaptureSession new];
        AVCaptureVideoDataOutput *out = [AVCaptureVideoDataOutput new];
        // 受け取る形式は NV12 (映像範囲)。カメラの形式が違えば AVFoundation が変換する
        out.videoSettings = @{(id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)};
        out.alwaysDiscardsLateVideoFrames = YES;
        [out setSampleBufferDelegate:cam queue:cam.queue];
        if (![s canAddInput:input] || ![s canAddOutput:out]) {
            set_err(err, errlen, @"カメラを読み取りに使えません");
            return NULL;
        }
        [s addInput:input];
        [s addOutput:out];
        cam.session = s;
        // 形式は動き始めてから選ぶ (始める前に選ぶと、始めたときにセッションの既定 (1080p など) へ戻される)
        [s startRunning];
        if (![dev lockForConfiguration:&e]) {
            [s stopRunning];
            set_err(err, errlen, e.localizedDescription ?: @"カメラの設定を変えられません");
            return NULL;
        }
        @try {
            dev.activeFormat = chosen;
            dev.activeVideoMinFrameDuration = range.minFrameDuration;
            dev.activeVideoMaxFrameDuration = range.minFrameDuration;
        } @catch (NSException *x) {
            [dev unlockForConfiguration];
            [s stopRunning];
            set_err(err, errlen, x.reason ?: @"形式を選べません");
            return NULL;
        }
        [dev unlockForConfiguration];
        cam.fps = range.maxFrameRate;
        return (__bridge_retained void *)cam;
    }
}

// 選んだ形式の fps
EXPORT double zvp_cam_fps(void *h) {
    return h ? ((__bridge ZvpCam *)h).fps : 0.0;
}

// last より新しいコマを最長 timeout 秒待つ。新しいコマの番号と大きさ・時刻、届かなければ 0、カメラが外れたら -1
EXPORT int64_t zvp_cam_wait(void *h, int64_t last, double timeout, int *width, int *height, double *time) {
    if (!h) return -1;
    return [(__bridge ZvpCam *)h waitAfter:last timeout:timeout width:width height:height time:time];
}

// その名前のカメラを AVFoundation で見つけられるか (見つからなければ FFmpeg で読む)
EXPORT int zvp_cam_exists(const char *name) {
    @autoreleasepool {
        return find_camera(name) ? 1 : 0;
    }
}

// 最新のコマを NV12 で dst へ写す (width×height に切って)。写したコマの番号、写せなければ -1、まだ無ければ 0
EXPORT int64_t zvp_cam_copy(void *h, uint8_t *dst, int64_t size, int width, int height) {
    if (!h) return -1;
    return [(__bridge ZvpCam *)h copyTo:dst size:size width:width height:height];
}

EXPORT void zvp_cam_close(void *h) {
    if (!h) return;
    @autoreleasepool {
        ZvpCam *cam = (__bridge_transfer ZvpCam *)h;
        [cam.session stopRunning];
        for (AVCaptureOutput *o in cam.session.outputs) {
            if ([o isKindOfClass:[AVCaptureVideoDataOutput class]])
                [(AVCaptureVideoDataOutput *)o setSampleBufferDelegate:nil queue:NULL];
        }
        dispatch_sync(cam.queue, ^{});     // 届きかけのコマを終わらせてから片付ける
    }
}

// ---------- カメラのホワイトバランス (UVC の Processing Unit) ----------
// macOS には UVC カメラの設定を変える仕組みが無いので、USB の制御要求を直接送る。カメラの使用許可は要らない。
// カメラは AVFoundation の名前で探し (find_camera)、modelID (「UVC Camera VendorID_13028 ProductID_26232」) の番号と、
// uniqueID (「0x124000032e46678」= 位置 0x12400000 + 番号) の位置で USB の機器を見つける。
#import <IOKit/IOKitLib.h>
#import <IOKit/IOCFPlugIn.h>
#import <IOKit/usb/IOUSBLib.h>

enum { UVC_SET_CUR = 0x01, UVC_GET_CUR = 0x81, UVC_GET_MIN = 0x82, UVC_GET_MAX = 0x83, UVC_GET_DEF = 0x87 };
enum { PU_WB_TEMPERATURE = 0x0A, PU_WB_TEMPERATURE_AUTO = 0x0B };

typedef struct {
    IOUSBDeviceInterface **dev;
    UInt16 index;            // wIndex: (Processing Unit の番号 << 8) | VideoControl のインターフェース番号
    UInt32 controls;         // Processing Unit の bmControls
} Uvc;


static IOReturn uvc_request(Uvc *u, UInt8 type, UInt8 request, UInt8 selector, void *buf, UInt16 len) {
    IOUSBDevRequest q = {type, request, (UInt16)(selector << 8), u->index, len, buf, 0};
    return (*u->dev)->DeviceRequest(u->dev, &q);
}

static void uvc_close(Uvc *u) {
    if (u->dev) (*u->dev)->Release(u->dev);
    u->dev = NULL;
}

// カメラの USB 機器を開き、構成の記述子から VideoControl と Processing Unit を探す
static BOOL uvc_open(const char *name, Uvc *u) {
    memset(u, 0, sizeof(*u));
    AVCaptureDevice *cam = find_camera(name);
    if (!cam) return NO;
    int vid = 0, pid = 0;
    if (sscanf(cam.modelID.UTF8String ?: "", "UVC Camera VendorID_%d ProductID_%d", &vid, &pid) != 2) return NO;
    unsigned long long uid = strtoull(cam.uniqueID.UTF8String ?: "", NULL, 16);
    UInt32 location = (UInt32)(uid >> 32);
    CFMutableDictionaryRef match = IOServiceMatching(kIOUSBDeviceClassName);
    CFDictionarySetValue(match, CFSTR(kUSBVendorID), (__bridge CFNumberRef)@(vid));
    CFDictionarySetValue(match, CFSTR(kUSBProductID), (__bridge CFNumberRef)@(pid));
    io_iterator_t it;
    if (IOServiceGetMatchingServices(MACH_PORT_NULL,   // 既定のポート (macOS 11 でも使える書き方)
                                     match, &it) != KERN_SUCCESS) return NO;
    io_service_t svc, chosen = 0;
    while ((svc = IOIteratorNext(it))) {       // 同じ機種が 2 台あれば、位置で見分ける
        NSNumber *loc = CFBridgingRelease(IORegistryEntryCreateCFProperty(svc, CFSTR(kUSBDevicePropertyLocationID),
                                                                          kCFAllocatorDefault, 0));
        if (!chosen || (location && loc.unsignedIntValue == location)) {
            if (chosen) IOObjectRelease(chosen);
            chosen = svc;
        } else {
            IOObjectRelease(svc);
        }
    }
    IOObjectRelease(it);
    if (!chosen) return NO;
    IOCFPlugInInterface **plug = NULL;
    SInt32 score;
    kern_return_t kr = IOCreatePlugInInterfaceForService(chosen, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID,
                                                         &plug, &score);
    IOObjectRelease(chosen);
    if (kr != KERN_SUCCESS || !plug) return NO;
    (*plug)->QueryInterface(plug, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID), (LPVOID *)&u->dev);
    (*plug)->Release(plug);
    if (!u->dev) return NO;
    UInt8 head[9];
    IOUSBDevRequest q = {0x80, kUSBRqGetDescriptor, kUSBConfDesc << 8, 0, sizeof(head), head, 0};
    if ((*u->dev)->DeviceRequest(u->dev, &q) != kIOReturnSuccess) { uvc_close(u); return NO; }
    UInt16 total = (UInt16)(head[2] | head[3] << 8);
    NSMutableData *cfg = [NSMutableData dataWithLength:total];
    IOUSBDevRequest q2 = {0x80, kUSBRqGetDescriptor, kUSBConfDesc << 8, 0, total, cfg.mutableBytes, 0};
    if ((*u->dev)->DeviceRequest(u->dev, &q2) != kIOReturnSuccess) { uvc_close(u); return NO; }
    const UInt8 *d = cfg.bytes;
    int iface = -1, cls = -1, sub = -1, unit = -1;
    for (UInt32 i = 0; i + 2 < q2.wLenDone && d[i] >= 2; i += d[i]) {
        if (d[i + 1] == kUSBInterfaceDesc && d[i] >= 9) {
            iface = d[i + 2]; cls = d[i + 5]; sub = d[i + 6];
        } else if (d[i + 1] == 0x24 && cls == 14 && sub == 1 && d[i + 2] == 5 && unit < 0 && d[i] >= 8) {
            unit = d[i + 3];                      // VideoControl の中の Processing Unit
            u->index = (UInt16)(unit << 8 | iface);
            for (int b = 0; b < d[i + 7] && b < 4 && i + 8 + b < q2.wLenDone; b++) u->controls |= (UInt32)d[i + 8 + b] << (8 * b);
        }
    }
    if (unit < 0) { uvc_close(u); return NO; }
    return YES;
}

static int uvc_get16(Uvc *u, UInt8 request, UInt8 selector, int *value) {
    UInt8 b[2] = {0};
    if (uvc_request(u, 0xA1, request, selector, b, 2) != kIOReturnSuccess) return 0;
    *value = b[0] | b[1] << 8;
    return 1;
}

// カメラのホワイトバランスの状態。使えなければ 0。has_auto: 自動があるか、auto_on: 自動が入っているか、
// t_*: 色温度 (K) の最小・最大・今・初期値 (色温度が無ければ 0)
EXPORT int zvp_cam_wb_info(const char *name, int *has_auto, int *auto_on, int *t_min, int *t_max, int *t_cur, int *t_def) {
    @autoreleasepool {
        Uvc u;
        if (!uvc_open(name, &u)) return 0;
        *has_auto = *auto_on = *t_min = *t_max = *t_cur = *t_def = 0;
        if (u.controls & (1u << 12)) {
            UInt8 v = 0;
            if (uvc_request(&u, 0xA1, UVC_GET_CUR, PU_WB_TEMPERATURE_AUTO, &v, 1) == kIOReturnSuccess) {
                *has_auto = 1;
                *auto_on = v ? 1 : 0;
            }
        }
        if (u.controls & (1u << 6)) {
            if (!(uvc_get16(&u, UVC_GET_MIN, PU_WB_TEMPERATURE, t_min) && uvc_get16(&u, UVC_GET_MAX, PU_WB_TEMPERATURE, t_max) &&
                  uvc_get16(&u, UVC_GET_CUR, PU_WB_TEMPERATURE, t_cur))) {
                *t_min = *t_max = *t_cur = 0;
            }
            uvc_get16(&u, UVC_GET_DEF, PU_WB_TEMPERATURE, t_def);
        }
        uvc_close(&u);
        return *has_auto || *t_max ? 1 : 0;
    }
}

// ホワイトバランスを自動 (auto_on = 1) にするか、色温度 temperature (K) に固定する。できたら 1
EXPORT int zvp_cam_wb_set(const char *name, int auto_on, int temperature) {
    @autoreleasepool {
        Uvc u;
        if (!uvc_open(name, &u)) return 0;
        int ok = 1;
        if (u.controls & (1u << 12)) {
            UInt8 v = auto_on ? 1 : 0;
            ok = uvc_request(&u, 0x21, UVC_SET_CUR, PU_WB_TEMPERATURE_AUTO, &v, 1) == kIOReturnSuccess;
        } else if (auto_on) {
            ok = 0;
        }
        if (ok && !auto_on && temperature > 0) {
            UInt8 b[2] = {(UInt8)(temperature & 0xff), (UInt8)(temperature >> 8)};
            ok = uvc_request(&u, 0x21, UVC_SET_CUR, PU_WB_TEMPERATURE, b, 2) == kIOReturnSuccess;
        }
        uvc_close(&u);
        return ok;
    }
}
