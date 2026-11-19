// macOS のカメラを AVFoundation で直接読むライブラリ (python/player_capture.py が ctypes で使う)。
//
// FFmpeg (avfoundation) は、同じ大きさの形式が 2 つ (MJPEG を展開した 420v と、圧縮しない yuvs など) あると
// 一覧の後ろのものを選び、形式の種類も指定できない。USB カメラによっては後ろの形式ではコマが届かず
// (4K U3 Camera の 4K は yuvs が後ろで、yuvs では 1 コマも来ない)、開くところで止まる。
// ここでは大きさと fps が合う形式のうち 420v / 420f (NV12) を優先して選び、NV12 で受け取る。
//
// コマは AVFoundation のスレッドで届く。最新の 1 枚だけを持っておき、Python が番号を見て新しければ写す。
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

@interface ZvpCam : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>
@property(nonatomic, strong) AVCaptureSession *session;
@property(nonatomic, strong) AVCaptureDevice *device;
@property(nonatomic, strong) dispatch_queue_t queue;
@property(nonatomic) double fps;
@end

@implementation ZvpCam {
    CVPixelBufferRef _latest;       // 最新のコマ (lock で守る)
    int64_t _seq;                   // 届いたコマの番号 (1 から)
    double _time;                   // 最新のコマの時刻 (秒)
    NSLock *_lock;
}

- (instancetype)init {
    if ((self = [super init])) {
        _lock = [NSLock new];
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
    [_lock lock];
    if (_latest) CVPixelBufferRelease(_latest);
    _latest = px;
    _seq += 1;
    _time = CMTIME_IS_VALID(t) ? CMTimeGetSeconds(t) : 0.0;
    [_lock unlock];
}

// 最新のコマの番号・大きさ・時刻。まだ無ければ 0
- (int64_t)latestWidth:(int *)w height:(int *)h time:(double *)time {
    [_lock lock];
    int64_t seq = _seq;
    if (_latest) {
        *w = (int)CVPixelBufferGetWidth(_latest);
        *h = (int)CVPixelBufferGetHeight(_latest);
        *time = _time;
    }
    [_lock unlock];
    return _latest ? seq : 0;
}

// 最新のコマを NV12 (Y を w×h、続けて CbCr を w×h/2。行の詰め物なし) で dst へ写す。写したコマの番号を返す
- (int64_t)copyTo:(uint8_t *)dst size:(int64_t)size width:(int)w height:(int)h {
    [_lock lock];
    CVPixelBufferRef px = _latest ? CVPixelBufferRetain(_latest) : NULL;
    int64_t seq = _seq;
    [_lock unlock];
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
        // FFmpeg の一覧の名前は前後の空白が落ちている (「4K U3 Camera 」が「4K U3 Camera」になる) のでそろえて比べる
        NSCharacterSet *space = [NSCharacterSet whitespaceAndNewlineCharacterSet];
        NSString *want = [[NSString stringWithUTF8String:name ?: ""] stringByTrimmingCharactersInSet:space];
        AVCaptureDevice *dev = nil;
        for (AVCaptureDevice *d in all_cameras()) {
            if ([[d.localizedName stringByTrimmingCharactersInSet:space] isEqualToString:want]) { dev = d; break; }
        }
        if (!dev) {
            set_err(err, errlen, [NSString stringWithFormat:@"カメラが見つかりません: %@", want]);
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

// 最新のコマの番号 (まだ無ければ 0) と大きさ・時刻
EXPORT int64_t zvp_cam_latest(void *h, int *width, int *height, double *time) {
    if (!h) return 0;
    return [(__bridge ZvpCam *)h latestWidth:width height:height time:time];
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
