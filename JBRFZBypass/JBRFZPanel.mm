#import "JBRFZPanel.h"

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>

#include <time.h>

// MARK: - File log (same Documents path as tweak)

static NSString *JbrfzPanelLocalTimestamp(void) {
    const time_t now = time(nullptr);
    struct tm localTime = {};
    if (localtime_r(&now, &localTime) == nullptr) {
        return @"<time-error>";
    }
    char buffer[40] = {};
    if (strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S %z",
                 &localTime) == 0) {
        return @"<time-error>";
    }
    return [NSString stringWithUTF8String:buffer];
}

static void JbrfzPanelLog(NSString *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    NSString *message = [[NSString alloc] initWithFormat:fmt arguments:args];
    va_end(args);
    NSLog(@"%@", message);

    NSString *home = NSHomeDirectory();
    if (home.length == 0) {
        return;
    }
    NSString *path =
        [home stringByAppendingPathComponent:@"Documents/jbrfzbypass.log"];
    NSString *line = [NSString stringWithFormat:@"%@ %@\n",
                                                JbrfzPanelLocalTimestamp(),
                                                message];
    NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
    @try {
        NSFileManager *fm = [NSFileManager defaultManager];
        if (![fm fileExistsAtPath:path]) {
            [fm createFileAtPath:path contents:data attributes:nil];
            return;
        }
        NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:path];
        if (fh == nil) {
            return;
        }
        [fh seekToEndOfFile];
        [fh writeData:data];
        [fh closeFile];
    } @catch (__unused NSException *e) {
    }
}

// MARK: - Defaults / master switch

static NSString *const kJbrfzAutoFeaturesKey = @"JBRFZBypass.autoFeaturesEnabled";
static NSString *const kJbrfzUnitySpeedKey = @"JBRFZBypass.unitySpeed3xEnabled";
static NSString *const kJbrfzFloatingEdgeLeftKey = @"JBRFZBypass.floatingEdgeLeft";
static NSString *const kJbrfzFloatingYRatioKey = @"JBRFZBypass.floatingYRatio";

static const NSTimeInterval kFloatingCollapseDelay = 2.5;
static const CGFloat kFloatingButtonSize = 48.0;
static const CGFloat kFloatingCollapsedVisible = 0.36;
static const CGFloat kFloatingCollapsedAlpha = 0.55;

// Default OFF so new accounts are not auto-driven.
static bool gAutoFeaturesEnabled = false;
static bool gAutoFeaturesLoaded = false;
static bool gUnitySpeedLoaded = false;

static void JbrfzLoadAutoFeaturesDefault(void) {
    if (gAutoFeaturesLoaded) {
        return;
    }
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    if ([defaults objectForKey:kJbrfzAutoFeaturesKey] == nil) {
        gAutoFeaturesEnabled = false;
        [defaults setBool:NO forKey:kJbrfzAutoFeaturesKey];
        [defaults synchronize];
    } else {
        gAutoFeaturesEnabled = [defaults boolForKey:kJbrfzAutoFeaturesKey];
    }
    gAutoFeaturesLoaded = true;
}

bool JbrfzAutoFeaturesEnabled(void) {
#if defined(JBRFZ_NO_INLINE_HOOKS) && \
    !defined(JBRFZ_STATIC_DISPATCH_HOOKS)
    return false;
#else
    JbrfzLoadAutoFeaturesDefault();
    return gAutoFeaturesEnabled;
#endif
}

static void JbrfzLoadUnitySpeedDefault(void) {
    if (gUnitySpeedLoaded) {
        return;
    }
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    const bool enabled = [defaults objectForKey:kJbrfzUnitySpeedKey] != nil &&
                         [defaults boolForKey:kJbrfzUnitySpeedKey];
    if ([defaults objectForKey:kJbrfzUnitySpeedKey] == nil) {
        [defaults setBool:NO forKey:kJbrfzUnitySpeedKey];
        [defaults synchronize];
    }
    gUnitySpeedLoaded = true;
    // Global state starts disabled. Avoid touching Unity just to restate 1x;
    // only an enabled preference needs an initial call/pulse schedule.
    if (enabled) {
        JbrfzSetUnitySpeedEnabled(true);
    }
}

static void JbrfzSetUnitySpeedPreference(bool enabled) {
    JbrfzSetUnitySpeedEnabled(enabled);
    [NSUserDefaults.standardUserDefaults setBool:enabled
                                          forKey:kJbrfzUnitySpeedKey];
    [NSUserDefaults.standardUserDefaults synchronize];
    JbrfzPanelLog(@"[JBRFZBypass] Unity speed %@",
                  enabled ? @"3X" : @"NORMAL");
}

static void JbrfzSetAutoFeaturesEnabled(bool enabled, bool persist) {
#if defined(JBRFZ_NO_INLINE_HOOKS) && \
    !defined(JBRFZ_STATIC_DISPATCH_HOOKS)
    (void)enabled;
    (void)persist;
    gAutoFeaturesEnabled = false;
    JbrfzPanelLog(@"[JBRFZBypass] Auto features unavailable in iOS safe mode");
    return;
#else
    JbrfzLoadAutoFeaturesDefault();
    const bool previous = gAutoFeaturesEnabled;
    gAutoFeaturesEnabled = enabled;
    if (persist) {
        [NSUserDefaults.standardUserDefaults setBool:enabled
                                              forKey:kJbrfzAutoFeaturesKey];
        [NSUserDefaults.standardUserDefaults synchronize];
    }
    if (previous && !enabled) {
        JbrfzCancelAutoActions("master-switch-off");
    }
    JbrfzPanelLog(@"[JBRFZBypass] Auto features %@",
                  enabled ? @"ENABLED" : @"DISABLED");
#endif
}

// MARK: - Passthrough window (Unity-safe overlay)

@interface JBRFZPassthroughWindow : UIWindow
@end

@implementation JBRFZPassthroughWindow
- (UIView *)hitTest:(CGPoint)point withEvent:(UIEvent *)event {
    UIView *hit = [super hitTest:point withEvent:event];
    if (hit == self || hit == self.rootViewController.view) {
        return nil; // let Unity receive touches outside controls
    }
    return hit;
}
@end

@interface JBRFZRootViewController : UIViewController
@property(nonatomic, weak) UIWindow *orientationSourceWindow;
@property(nonatomic, copy) void (^layoutHandler)(void);
@end

@implementation JBRFZRootViewController

static UIInterfaceOrientationMask JbrfzMaskForInterfaceOrientation(
    UIInterfaceOrientation orientation) {
    switch (orientation) {
        case UIInterfaceOrientationLandscapeLeft:
            return UIInterfaceOrientationMaskLandscapeLeft;
        case UIInterfaceOrientationLandscapeRight:
            return UIInterfaceOrientationMaskLandscapeRight;
        case UIInterfaceOrientationPortraitUpsideDown:
            return UIInterfaceOrientationMaskPortraitUpsideDown;
        case UIInterfaceOrientationPortrait:
        default:
            return UIInterfaceOrientationMaskPortrait;
    }
}

- (UIViewController *)orientationSourceViewController {
    UIWindow *sourceWindow = self.orientationSourceWindow;
    if (sourceWindow == nil || sourceWindow == self.view.window) {
        return nil;
    }
    return sourceWindow.rootViewController;
}

- (BOOL)prefersStatusBarHidden {
    return NO;
}
- (UIStatusBarStyle)preferredStatusBarStyle {
    return UIStatusBarStyleLightContent;
}
- (BOOL)shouldAutorotate {
    UIViewController *source = [self orientationSourceViewController];
    if (source != nil && [source respondsToSelector:_cmd]) {
        return [source shouldAutorotate];
    }
    // A standalone overlay must never be the reason the scene starts
    // rotating. If the game root cannot be found, keep the current scene
    // orientation instead of advertising all orientations.
    return NO;
}
- (UIInterfaceOrientationMask)supportedInterfaceOrientations {
    UIViewController *source = [self orientationSourceViewController];
    if (source != nil && [source respondsToSelector:_cmd]) {
        UIInterfaceOrientationMask mask = [source supportedInterfaceOrientations];
        if (mask != 0) {
            return mask;
        }
    }

    if (@available(iOS 13.0, *)) {
        UIWindowScene *scene = self.view.window.windowScene;
        if (scene != nil &&
            scene.interfaceOrientation != UIInterfaceOrientationUnknown) {
            return JbrfzMaskForInterfaceOrientation(scene.interfaceOrientation);
        }
    }
    return UIInterfaceOrientationMaskPortrait;
}

- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    if (self.layoutHandler != nil) {
        self.layoutHandler();
    }
}
@end

// MARK: - Overlay controller

@interface JBRFZPluginOverlay : NSObject
@property(nonatomic, strong) JBRFZPassthroughWindow *overlayWindow;
@property(nonatomic, strong) UIButton *floatingButton;
@property(nonatomic, strong) UIView *panel;
@property(nonatomic, strong) UISwitch *autoSwitch;
@property(nonatomic, strong) UISwitch *speedSwitch;
@property(nonatomic, strong) UILabel *statusLabel;
@property(nonatomic, assign) BOOL floatingButtonWasDragged;
@property(nonatomic, assign) BOOL floatingExpandedOnTouch;
@property(nonatomic, assign) BOOL floatingCollapsed;
@property(nonatomic, assign) BOOL floatingPreferLeft;
@property(nonatomic, assign) NSUInteger floatingDockGeneration;
@property(nonatomic, assign) NSUInteger installAttempts;
+ (instancetype)sharedOverlay;
- (void)installWhenReady;
- (void)syncOrientationSource;
- (void)refreshUI;
@end

@implementation JBRFZPluginOverlay

+ (instancetype)sharedOverlay {
    static JBRFZPluginOverlay *overlay;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        overlay = [JBRFZPluginOverlay new];
        overlay.floatingPreferLeft = NO;
    });
    return overlay;
}

- (UIWindowScene *)activeWindowScene API_AVAILABLE(ios(13.0)) {
    UIWindowScene *fallback = nil;
    for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:UIWindowScene.class]) {
            continue;
        }
        UIWindowScene *ws = (UIWindowScene *)scene;
        if (ws.activationState == UISceneActivationStateForegroundActive) {
            return ws;
        }
        if (fallback == nil &&
            (ws.activationState == UISceneActivationStateForegroundInactive ||
             ws.activationState == UISceneActivationStateForegroundActive)) {
            fallback = ws;
        }
        if (fallback == nil) {
            fallback = ws;
        }
    }
    return fallback;
}

- (void)retryInstallAfterDelay:(NSTimeInterval)delay reason:(NSString *)reason {
    self.installAttempts += 1;
    if (self.installAttempts > 60) {
        JbrfzPanelLog(@"[JBRFZBypass] Panel install gave up after %lu tries (%@)",
                      (unsigned long)self.installAttempts, reason ?: @"?");
        return;
    }
    if (self.installAttempts <= 5 || (self.installAttempts % 5) == 0) {
        JbrfzPanelLog(@"[JBRFZBypass] Panel retry #%lu in %.1fs (%@)",
                      (unsigned long)self.installAttempts, delay,
                      reason ?: @"?");
    }
    __weak typeof(self) weakSelf = self;
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(delay * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
            [weakSelf installWhenReady];
        });
}

- (UIWindow *)gameWindowForScene:(UIWindowScene *)scene {
    if (scene == nil) {
        return nil;
    }

    UIWindow *fallback = nil;
    for (UIWindow *candidate in scene.windows) {
        if (candidate == self.overlayWindow || candidate.hidden ||
            candidate.rootViewController == nil || candidate.alpha <= 0.01) {
            continue;
        }
        if (candidate.isKeyWindow) {
            return candidate;
        }
        if (fallback == nil) {
            fallback = candidate;
        }
    }
    return fallback;
}

- (void)syncOrientationSource {
    if (self.overlayWindow == nil ||
        ![self.overlayWindow.rootViewController
            isKindOfClass:JBRFZRootViewController.class]) {
        return;
    }

    JBRFZRootViewController *root =
        (JBRFZRootViewController *)self.overlayWindow.rootViewController;
    UIWindow *gameWindow = nil;
    if (@available(iOS 13.0, *)) {
        gameWindow = [self gameWindowForScene:self.overlayWindow.windowScene];
    } else {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        for (UIWindow *candidate in UIApplication.sharedApplication.windows) {
            if (candidate != self.overlayWindow && !candidate.hidden &&
                candidate.rootViewController != nil) {
                gameWindow = candidate;
                if (candidate.isKeyWindow) {
                    break;
                }
            }
        }
#pragma clang diagnostic pop
    }

    root.orientationSourceWindow = gameWindow;
    __weak typeof(self) weakSelf = self;
    root.layoutHandler = ^{
        __strong typeof(weakSelf) self = weakSelf;
        if (self == nil) {
            return;
        }
        [self positionPanel];
        [self restoreFloatingButtonPositionAnimated:NO
                                           collapsed:self.floatingCollapsed];
    };

    if (@available(iOS 16.0, *)) {
        [root setNeedsUpdateOfSupportedInterfaceOrientations];
    }
}

- (void)installWhenReady {
    NSAssert(NSThread.isMainThread,
             @"JBRFZPluginOverlay must be installed on main thread");

    UIApplication *app = UIApplication.sharedApplication;
    if (app == nil) {
        [self retryInstallAfterDelay:0.5 reason:@"no-UIApplication"];
        return;
    }

    // Already installed: re-front.
    if (self.overlayWindow != nil && self.floatingButton != nil) {
        self.overlayWindow.hidden = NO;
        [self.overlayWindow makeKeyAndVisible];
        // Immediately resign key so game keeps keyboard/input focus.
        // keep our window visible at high level without being key.
        // makeKeyAndVisible can steal focus on Unity; use hidden=NO only after first show.
        self.overlayWindow.windowLevel = UIWindowLevelStatusBar + 120.0;
        [self.overlayWindow bringSubviewToFront:self.panel];
        [self.overlayWindow bringSubviewToFront:self.floatingButton];
        [self syncOrientationSource];
        [self restoreFloatingButtonPositionAnimated:NO
                                          collapsed:self.floatingCollapsed];
        return;
    }

    CGRect bounds = CGRectZero;
    JBRFZPassthroughWindow *window = nil;

    if (@available(iOS 13.0, *)) {
        UIWindowScene *scene = [self activeWindowScene];
        if (scene == nil) {
            [self retryInstallAfterDelay:0.6 reason:@"no-window-scene"];
            return;
        }
        bounds = scene.coordinateSpace.bounds;
        if (bounds.size.width < 32.0 || bounds.size.height < 32.0) {
            [self retryInstallAfterDelay:0.6 reason:@"scene-bounds-too-small"];
            return;
        }
        window = [[JBRFZPassthroughWindow alloc] initWithWindowScene:scene];
        window.frame = bounds;
    } else {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        bounds = UIScreen.mainScreen.bounds;
#pragma clang diagnostic pop
        if (bounds.size.width < 32.0 || bounds.size.height < 32.0) {
            [self retryInstallAfterDelay:0.6 reason:@"screen-bounds-too-small"];
            return;
        }
        window = [[JBRFZPassthroughWindow alloc] initWithFrame:bounds];
    }

    UIWindow *gameWindow = nil;
    if (@available(iOS 13.0, *)) {
        gameWindow = [self gameWindowForScene:window.windowScene];
    }

    window.windowLevel = UIWindowLevelStatusBar + 120.0;
    window.backgroundColor = UIColor.clearColor;
    window.opaque = NO;
    window.userInteractionEnabled = YES;
    JBRFZRootViewController *root = [JBRFZRootViewController new];
    root.orientationSourceWindow = gameWindow;
    window.rootViewController = root;
    window.rootViewController.view.backgroundColor = UIColor.clearColor;

    UIView *host = window.rootViewController.view;

    UIView *panel = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 300, 280)];
    panel.backgroundColor = [UIColor colorWithWhite:0.08 alpha:0.96];
    panel.layer.cornerRadius = 16.0;
    panel.layer.borderWidth = 1.0;
    panel.layer.borderColor = [UIColor colorWithWhite:1.0 alpha:0.22].CGColor;
    panel.clipsToBounds = YES;
    panel.hidden = YES;
    panel.accessibilityIdentifier = @"JBRFZBypass.pluginPanel";

    UILabel *title = [[UILabel alloc] initWithFrame:CGRectMake(18, 14, 210, 25)];
    title.text = @"JBRFZ 插件";
    title.textColor = UIColor.whiteColor;
    title.font = [UIFont boldSystemFontOfSize:17.0];
    [panel addSubview:title];

    UIButton *close = [UIButton buttonWithType:UIButtonTypeSystem];
    close.frame = CGRectMake(250, 10, 40, 32);
    [close setTitle:@"✕" forState:UIControlStateNormal];
    [close setTitleColor:UIColor.whiteColor forState:UIControlStateNormal];
    close.titleLabel.font =
        [UIFont systemFontOfSize:18.0 weight:UIFontWeightSemibold];
    [close addTarget:self
                  action:@selector(togglePanel)
        forControlEvents:UIControlEventTouchUpInside];
    [panel addSubview:close];

    UILabel *autoLabel =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 58, 210, 28)];
#if defined(JBRFZ_STATIC_DISPATCH_HOOKS)
    autoLabel.text = @"启用自动功能（静态签名）";
#elif defined(JBRFZ_NO_INLINE_HOOKS)
    autoLabel.text = @"自动功能（安全模式不可用）";
#else
    autoLabel.text = @"启用自动功能";
#endif
    autoLabel.textColor = UIColor.whiteColor;
    autoLabel.font = [UIFont systemFontOfSize:16.0 weight:UIFontWeightMedium];
    [panel addSubview:autoLabel];

    UILabel *autoDetail =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 88, 230, 54)];
#if defined(JBRFZ_STATIC_DISPATCH_HOOKS)
    autoDetail.text = @"自动功能使用签名前静态跳板，不在运行时修改游戏代码页。\n"
                      @"新手号请保持关闭。";
#elif defined(JBRFZ_NO_INLINE_HOOKS)
    autoDetail.text = @"iOS 27 会拦截运行时内联 Hook，当前仅启用可安全运行的功能。";
#else
    autoDetail.text = @"仅自动执行序列任务；活动任务始终保持手动。\n"
                      @"关闭后序列任务也不自动执行。";
#endif
    autoDetail.textColor = [UIColor colorWithWhite:0.78 alpha:1.0];
    autoDetail.font = [UIFont systemFontOfSize:12.0];
    autoDetail.numberOfLines = 0;
    [panel addSubview:autoDetail];

    UISwitch *autoSwitch = [[UISwitch alloc] initWithFrame:CGRectZero];
    autoSwitch.on = JbrfzAutoFeaturesEnabled();
#if defined(JBRFZ_NO_INLINE_HOOKS) && \
    !defined(JBRFZ_STATIC_DISPATCH_HOOKS)
    autoSwitch.enabled = NO;
#endif
    autoSwitch.onTintColor =
        [UIColor colorWithRed:0.19 green:0.68 blue:1.0 alpha:1.0];
    [autoSwitch addTarget:self
                   action:@selector(autoSwitchChanged:)
         forControlEvents:UIControlEventValueChanged];
    [panel addSubview:autoSwitch];
    self.autoSwitch = autoSwitch;

    UILabel *speedLabel =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 148, 210, 28)];
    speedLabel.text = @"Unity 加速（固定 3×）";
    speedLabel.textColor = UIColor.whiteColor;
    speedLabel.font = [UIFont systemFontOfSize:16.0 weight:UIFontWeightMedium];
    [panel addSubview:speedLabel];

    UISwitch *speedSwitch = [[UISwitch alloc] initWithFrame:CGRectZero];
    speedSwitch.on = JbrfzUnitySpeedEnabled();
    speedSwitch.onTintColor =
        [UIColor colorWithRed:0.96 green:0.55 blue:0.16 alpha:1.0];
    [speedSwitch addTarget:self
                    action:@selector(speedSwitchChanged:)
          forControlEvents:UIControlEventValueChanged];
    [panel addSubview:speedSwitch];
    self.speedSwitch = speedSwitch;

    UILabel *speedDetail =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 178, 250, 20)];
    speedDetail.text = @"仅适配游戏 1.1.101，关闭后恢复 1×。";
    speedDetail.textColor = [UIColor colorWithWhite:0.78 alpha:1.0];
    speedDetail.font = [UIFont systemFontOfSize:12.0];
    [panel addSubview:speedDetail];

    UILabel *status =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 218, 264, 42)];
    status.textColor = [UIColor colorWithWhite:0.72 alpha:1.0];
    status.font = [UIFont monospacedDigitSystemFontOfSize:12.0
                                                   weight:UIFontWeightRegular];
    status.numberOfLines = 2;
    [panel addSubview:status];
    self.statusLabel = status;

    UIButton *button = [UIButton buttonWithType:UIButtonTypeCustom];
    button.frame = CGRectMake(0, 0, kFloatingButtonSize, kFloatingButtonSize);
    button.backgroundColor =
        [UIColor colorWithRed:0.10 green:0.45 blue:0.95 alpha:0.95];
    button.layer.cornerRadius = kFloatingButtonSize * 0.5;
    button.layer.borderWidth = 2.0;
    button.layer.borderColor = UIColor.whiteColor.CGColor;
    button.layer.shadowColor = UIColor.blackColor.CGColor;
    button.layer.shadowOpacity = 0.45;
    button.layer.shadowRadius = 5.0;
    button.layer.shadowOffset = CGSizeMake(0.0, 2.0);
    [button setTitle:@"JB" forState:UIControlStateNormal];
    [button setTitleColor:UIColor.whiteColor forState:UIControlStateNormal];
    button.titleLabel.font = [UIFont boldSystemFontOfSize:14.0];
    button.accessibilityLabel = @"打开 JBRFZ 插件面板";
    button.accessibilityIdentifier = @"JBRFZBypass.floatingButton";
    [button addTarget:self
                  action:@selector(floatingButtonTouchDown:)
        forControlEvents:UIControlEventTouchDown];
    [button addTarget:self
                  action:@selector(floatingButtonTapped:)
        forControlEvents:UIControlEventTouchUpInside];

    UIPanGestureRecognizer *pan = [[UIPanGestureRecognizer alloc]
        initWithTarget:self
                action:@selector(dragFloatingButton:)];
    pan.cancelsTouchesInView = YES;
    [button addGestureRecognizer:pan];

    self.panel = panel;
    self.floatingButton = button;
    self.overlayWindow = window;
    [self syncOrientationSource];
    [host addSubview:panel];
    [host addSubview:button];

    // First show: make visible. Avoid permanently stealing key window.
    window.hidden = NO;
    // Need makeKeyAndVisible once so the layer composites on some iOS builds.
    [window makeKeyAndVisible];
    // Return key focus to a game window after a tick (don't keep ours key).
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.05 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
          UIWindow *gameKey = nil;
          if (@available(iOS 13.0, *)) {
              for (UIScene *scene in UIApplication.sharedApplication
                                         .connectedScenes) {
                  if (![scene isKindOfClass:UIWindowScene.class]) {
                      continue;
                  }
                  for (UIWindow *w in ((UIWindowScene *)scene).windows) {
                      if (w != window && !w.isHidden) {
                          gameKey = w;
                          break;
                      }
                  }
                  if (gameKey != nil) {
                      break;
                  }
              }
          }
          if (gameKey != nil) {
              [gameKey makeKeyWindow];
          }
        });

    self.floatingCollapsed = NO;
    self.floatingExpandedOnTouch = NO;
    [self restoreFloatingButtonPositionAnimated:NO collapsed:NO];
    [self positionPanel];
    [self refreshUI];
    // Keep expanded longer on first install so user can find it.
    [self scheduleFloatingCollapse];

    JbrfzPanelLog(
        @"[JBRFZBypass] Plugin panel installed bounds=%@ auto=%d level=%.0f",
        NSStringFromCGRect(bounds), JbrfzAutoFeaturesEnabled() ? 1 : 0,
        (double)window.windowLevel);
}

- (BOOL)isPluginPanelVisible {
    return self.panel && !self.panel.hidden;
}

- (UIView *)hostView {
    return self.overlayWindow.rootViewController.view;
}

- (CGRect)floatingDragBoundsCollapsed:(BOOL)collapsed {
    UIView *host = [self hostView];
    if (!host) {
        return CGRectZero;
    }
    UIEdgeInsets safe = host.safeAreaInsets;
    CGFloat half = kFloatingButtonSize * 0.5;
    CGFloat minY = safe.top + half + 6.0;
    CGFloat maxY = CGRectGetHeight(host.bounds) - safe.bottom - half - 6.0;
    if (maxY < minY) {
        minY = half + 6.0;
        maxY = CGRectGetHeight(host.bounds) - half - 6.0;
    }
    CGFloat minX;
    CGFloat maxX;
    if (collapsed) {
        CGFloat visible = kFloatingButtonSize * kFloatingCollapsedVisible;
        minX = visible - half;
        maxX = CGRectGetWidth(host.bounds) - (visible - half);
    } else {
        minX = safe.left + half + 6.0;
        maxX = CGRectGetWidth(host.bounds) - safe.right - half - 6.0;
    }
    if (maxX < minX) {
        minX = half;
        maxX = CGRectGetWidth(host.bounds) - half;
    }
    return CGRectMake(minX, minY, maxX - minX, maxY - minY);
}

- (CGPoint)clampFloatingCenter:(CGPoint)center collapsed:(BOOL)collapsed {
    CGRect box = [self floatingDragBoundsCollapsed:collapsed];
    center.x = MIN(MAX(center.x, CGRectGetMinX(box)), CGRectGetMaxX(box));
    center.y = MIN(MAX(center.y, CGRectGetMinY(box)), CGRectGetMaxY(box));
    return center;
}

- (CGPoint)edgeCenterPreferLeft:(BOOL)preferLeft
                              y:(CGFloat)y
                      collapsed:(BOOL)collapsed {
    CGRect box = [self floatingDragBoundsCollapsed:collapsed];
    CGPoint center;
    center.x = preferLeft ? CGRectGetMinX(box) : CGRectGetMaxX(box);
    center.y = y;
    return [self clampFloatingCenter:center collapsed:collapsed];
}

- (void)saveFloatingButtonPosition {
    UIButton *button = self.floatingButton;
    UIView *host = [self hostView];
    if (!button || !host) {
        return;
    }
    CGRect box = [self floatingDragBoundsCollapsed:NO];
    CGFloat span = MAX(1.0, CGRectGetHeight(box));
    CGFloat yRatio = (button.center.y - CGRectGetMinY(box)) / span;
    yRatio = MIN(MAX(yRatio, 0.0), 1.0);
    BOOL preferLeft = button.center.x < CGRectGetMidX(host.bounds);
    self.floatingPreferLeft = preferLeft;
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults setBool:preferLeft forKey:kJbrfzFloatingEdgeLeftKey];
    [defaults setDouble:yRatio forKey:kJbrfzFloatingYRatioKey];
    [defaults synchronize];
}

- (void)restoreFloatingButtonPositionAnimated:(BOOL)animated
                                    collapsed:(BOOL)collapsed {
    UIButton *button = self.floatingButton;
    UIView *host = [self hostView];
    if (!button || !host) {
        return;
    }
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    BOOL hasSaved = [defaults objectForKey:kJbrfzFloatingYRatioKey] != nil;
    BOOL preferLeft =
        hasSaved ? [defaults boolForKey:kJbrfzFloatingEdgeLeftKey] : NO;
    CGFloat yRatio =
        hasSaved ? [defaults doubleForKey:kJbrfzFloatingYRatioKey] : 0.22;
    yRatio = MIN(MAX(yRatio, 0.0), 1.0);
    self.floatingPreferLeft = preferLeft;

    CGRect box = [self floatingDragBoundsCollapsed:NO];
    CGFloat y = CGRectGetMinY(box) + yRatio * MAX(1.0, CGRectGetHeight(box));
    CGPoint center =
        [self edgeCenterPreferLeft:preferLeft y:y collapsed:collapsed];
    self.floatingCollapsed = collapsed;
    void (^apply)(void) = ^{
        button.center = center;
        button.alpha = collapsed ? kFloatingCollapsedAlpha : 1.0;
    };
    if (animated) {
        [UIView animateWithDuration:0.22
                              delay:0
                            options:UIViewAnimationOptionCurveEaseInOut
                         animations:apply
                         completion:nil];
    } else {
        apply();
    }
}

- (void)cancelFloatingCollapse {
    self.floatingDockGeneration += 1;
}

- (void)scheduleFloatingCollapse {
    if (!self.floatingButton || [self isPluginPanelVisible]) {
        [self cancelFloatingCollapse];
        return;
    }
    NSUInteger generation = ++self.floatingDockGeneration;
    __weak typeof(self) weakSelf = self;
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW,
                      (int64_t)(kFloatingCollapseDelay * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
            __strong typeof(weakSelf) self = weakSelf;
            if (!self) {
                return;
            }
            if (generation != self.floatingDockGeneration) {
                return;
            }
            if ([self isPluginPanelVisible]) {
                return;
            }
            [self setFloatingCollapsed:YES animated:YES];
        });
}

- (void)setFloatingCollapsed:(BOOL)collapsed animated:(BOOL)animated {
    UIButton *button = self.floatingButton;
    UIView *host = [self hostView];
    if (!button || !host) {
        return;
    }
    BOOL preferLeft = self.floatingPreferLeft;
    if (!collapsed || !self.floatingCollapsed) {
        preferLeft = button.center.x < CGRectGetMidX(host.bounds);
        self.floatingPreferLeft = preferLeft;
    }
    CGPoint center = [self edgeCenterPreferLeft:preferLeft
                                              y:button.center.y
                                      collapsed:collapsed];
    self.floatingCollapsed = collapsed;
    void (^apply)(void) = ^{
        button.center = center;
        button.alpha = collapsed ? kFloatingCollapsedAlpha : 1.0;
    };
    if (animated) {
        [UIView animateWithDuration:0.22
                              delay:0
                            options:UIViewAnimationOptionCurveEaseInOut
                         animations:apply
                         completion:nil];
    } else {
        apply();
    }
    if (!collapsed) {
        [self saveFloatingButtonPosition];
    }
}

- (void)snapFloatingButtonToEdgeAndSave {
    UIButton *button = self.floatingButton;
    UIView *host = [self hostView];
    if (!button || !host) {
        return;
    }
    BOOL preferLeft = button.center.x < CGRectGetMidX(host.bounds);
    self.floatingPreferLeft = preferLeft;
    self.floatingCollapsed = NO;
    CGPoint center = [self edgeCenterPreferLeft:preferLeft
                                              y:button.center.y
                                      collapsed:NO];
    [UIView animateWithDuration:0.18
                          delay:0
                        options:UIViewAnimationOptionCurveEaseOut
                     animations:^{
                         button.center = center;
                         button.alpha = 1.0;
                     }
                     completion:^(__unused BOOL finished) {
                         [self saveFloatingButtonPosition];
                         [self scheduleFloatingCollapse];
                     }];
}

- (void)positionPanel {
    UIView *host = [self hostView];
    if (!host || !self.panel) {
        return;
    }
    UIEdgeInsets safe = host.safeAreaInsets;
    CGFloat availableWidth =
        CGRectGetWidth(host.bounds) - safe.left - safe.right;
    CGFloat panelWidth = MIN(300.0, MAX(240.0, availableWidth - 24.0));
    CGFloat x = safe.left + (availableWidth - panelWidth) * 0.5;
    CGFloat y = safe.top + 74.0;
    CGFloat availableHeight =
        CGRectGetHeight(host.bounds) - safe.bottom - y - 12.0;
    CGFloat panelHeight = MIN(280.0, MAX(250.0, availableHeight));
    self.panel.frame = CGRectMake(x, y, panelWidth, panelHeight);
    self.autoSwitch.center = CGPointMake(panelWidth - 46.0, 72.0);
    self.speedSwitch.center = CGPointMake(panelWidth - 46.0, 162.0);
    self.statusLabel.frame = CGRectMake(18.0, 218.0, panelWidth - 36.0, 42.0);
}

- (void)refreshUI {
    const bool enabled = JbrfzAutoFeaturesEnabled();
    const bool speedEnabled = JbrfzUnitySpeedEnabled();
    self.autoSwitch.on = enabled;
    self.speedSwitch.on = speedEnabled;
#if defined(JBRFZ_STATIC_DISPATCH_HOOKS)
    self.statusLabel.text = [NSString
        stringWithFormat:@"状态：静态自动%@；Unity %@",
                         enabled ? @"开启" : @"关闭",
                         speedEnabled ? @"3×" : @"1×"];
#elif defined(JBRFZ_NO_INLINE_HOOKS)
    self.statusLabel.text = [NSString
        stringWithFormat:@"状态：iOS 安全模式；Unity %@",
                         speedEnabled ? @"3×" : @"1×"];
#else
    self.statusLabel.text = [NSString
        stringWithFormat:@"状态：自动%@；Unity %@",
                         enabled ? @"开启" : @"关闭",
                         speedEnabled ? @"3×" : @"1×"];
#endif
    self.floatingButton.accessibilityValue =
        enabled ? @"自动已开启" : @"自动已关闭";
    self.floatingButton.backgroundColor =
        enabled ? [UIColor colorWithRed:0.12 green:0.72 blue:0.38 alpha:0.95]
                : [UIColor colorWithRed:0.10 green:0.45 blue:0.95 alpha:0.95];
}

- (void)togglePanel {
    BOOL willShow = self.panel.hidden;
    self.panel.hidden = !willShow;
    if (willShow) {
        [self cancelFloatingCollapse];
        [self setFloatingCollapsed:NO animated:YES];
        [self positionPanel];
        [self refreshUI];
        [[self hostView] bringSubviewToFront:self.panel];
        [[self hostView] bringSubviewToFront:self.floatingButton];
    } else {
        [self scheduleFloatingCollapse];
    }
}

- (void)autoSwitchChanged:(UISwitch *)sender {
    JbrfzSetAutoFeaturesEnabled(sender.isOn, true);
    [self refreshUI];
}

- (void)speedSwitchChanged:(UISwitch *)sender {
    JbrfzSetUnitySpeedPreference(sender.isOn);
    [self refreshUI];
}

- (void)floatingButtonTouchDown:(UIButton *)sender {
    self.floatingButtonWasDragged = NO;
    [self cancelFloatingCollapse];
    if (self.floatingCollapsed) {
        self.floatingExpandedOnTouch = YES;
        [self setFloatingCollapsed:NO animated:YES];
    } else {
        self.floatingExpandedOnTouch = NO;
        sender.alpha = 1.0;
    }
}

- (void)floatingButtonTapped:(UIButton *)sender {
    (void)sender;
    if (self.floatingButtonWasDragged) {
        self.floatingButtonWasDragged = NO;
        self.floatingExpandedOnTouch = NO;
        [self scheduleFloatingCollapse];
        return;
    }
    if (self.floatingExpandedOnTouch) {
        self.floatingExpandedOnTouch = NO;
        [self scheduleFloatingCollapse];
        return;
    }
    [self togglePanel];
}

- (void)dragFloatingButton:(UIPanGestureRecognizer *)recognizer {
    UIView *button = recognizer.view;
    UIView *host = [self hostView];
    if (!button || !host) {
        return;
    }

    if (recognizer.state == UIGestureRecognizerStateBegan) {
        self.floatingButtonWasDragged = YES;
        self.floatingExpandedOnTouch = NO;
        [self cancelFloatingCollapse];
        if (self.floatingCollapsed) {
            [self setFloatingCollapsed:NO animated:NO];
        }
        button.alpha = 1.0;
    } else if (recognizer.state == UIGestureRecognizerStateChanged) {
        self.floatingButtonWasDragged = YES;
    }

    CGPoint translation = [recognizer translationInView:host];
    CGPoint center = button.center;
    center.x += translation.x;
    center.y += translation.y;
    [recognizer setTranslation:CGPointZero inView:host];
    button.center = [self clampFloatingCenter:center collapsed:NO];
    button.alpha = 1.0;
    self.floatingCollapsed = NO;

    if (recognizer.state == UIGestureRecognizerStateEnded ||
        recognizer.state == UIGestureRecognizerStateCancelled) {
        [self snapFloatingButtonToEdgeAndSave];
    }
}

@end

void JbrfzStartPluginPanel(void) {
    // Delay a bit: UIApplication/scenes may not exist at dylib load time.
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.0 * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
            JbrfzLoadAutoFeaturesDefault();
            JbrfzLoadUnitySpeedDefault();
            JBRFZPluginOverlay *overlay = JBRFZPluginOverlay.sharedOverlay;
            JbrfzPanelLog(@"[JBRFZBypass] Plugin panel schedule start auto=%d",
                          JbrfzAutoFeaturesEnabled() ? 1 : 0);
            [NSNotificationCenter.defaultCenter
                addObserverForName:UIApplicationDidBecomeActiveNotification
                            object:nil
                             queue:NSOperationQueue.mainQueue
                        usingBlock:^(__unused NSNotification *notification) {
                            [overlay installWhenReady];
                        }];
            if (@available(iOS 13.0, *)) {
                [NSNotificationCenter.defaultCenter
                    addObserverForName:UISceneDidActivateNotification
                                object:nil
                                 queue:NSOperationQueue.mainQueue
                            usingBlock:^(__unused NSNotification *n) {
                                [overlay installWhenReady];
                            }];
            }
            [overlay installWhenReady];
        });
}
