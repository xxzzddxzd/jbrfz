# JBRFZ Bypass

Rootless Theos tweak for `com.devsisters.cc` (CookieRun: Crumble).

## AppSealing

The app's `AppSealingSDK` starts a background integrity loop and treats a
non-zero result from its exported environment checks as an abnormal device.
The tweak resolves those exports from `UnityFramework` at runtime and makes
them report the normal value (`0`). For app version 1.0.101 (19), it also
disables the SDK's delayed managed monitor and exit callback after verifying
the exact main-executable and `UnityFramework` UUIDs.

## 免广告卡 (Ad Remove)

Game client "ad free card" state is driven by
`Crumble.AdRemoveGameBoostCalculator.IsAdRemoveActive()`.

That method walks active `GameBoost` records and returns true when any boost
has `GameBoostType.AdRemove` (`1`). Callers include:

- `AdService.get_IsRemovedAd`
- `AdService.ShowRewardedAsync` (skips real ad playback and reports success)
- advertisement / side-menu red-dot updaters
- stage random reward claim UI
- currency package cells

For the UUID-matched UnityFramework build, the tweak hooks
`IsAdRemoveActive` at RVA `0x03D85D74` and always returns `true`.

This is a client-side gate only. Server-owned inventory/boost records and
anti-cheat validation are unchanged.


## 错误弹框不回登录

业务 RPC 失败由 `MainSceneExchangeEventListener.AfterResponse` 弹出
系统提示框（默认文案 `ErrorCodeUnclassifiedErrorStr`，即
“处理请求过程中出现错误”）。点确定后进入
`AfterError`：未识别错误码或 `AfterActionType.Restart` 会调用
`GameSceneLoader.RestartApp()` 重载登录场景。

tweak 将 `AfterError`（RVA `0x03A46A54`）置空：弹框仍可关闭，
不再强制返回登录页。背景业务可继续跑。


## 导航任务 880 自动提交

导航任务由 `GuidePresenter` 驱动。条件完成后默认只刷新 UI，需
玩家点击才走 `ClaimGuideRewardAsync`（RPC ClearGuideAchievement /
ClearGuideRequirement）。

tweak hook `HandleOnProgressUpdated`（RVA `0x04215BEC`）：在原逻辑
更新完当前 guide 后，若当前是 guide `880` 且 `IsCompleted`，自动
调用 `ClaimGuideRewardAsync` 并 `Forget`，无需手动点提交。



## 重复任务击杀计数假完成（客户端）

`RequirementUpdaterBase.Register` 会在 `RequirementRecordSet` 里 Remove+Add 新
record，但不会对旧对象调用 `RemoveRecordByType`，`RecordsByType` 会残留幽灵
条目。type `130`（重复 guide 击杀）在 `unitCounts` 缺失时还会走
`CalculateCurrentValue` 的 NotImplemented 异常分支，导致旧记录无法被替换，
UI 可能提前显示 completed，点领取被服务器拒绝。

tweak（始终生效，不依赖自动任务开关）：

- hook `Register`（RVA `0x03E16814`）：注册前若已有同 id record，先调
  `RemoveRecordByType`（`0x03E163D8`）清 `RecordsByType`
- hook `CalculateCurrentValue`（RVA `0x03E1ADF4`）：type `129`/`130` 返回 0，
  避免注册失败

领取请求仍只带 guideId/类型，服务器独立校验；本补丁只修客户端显示与本地计数一致性。

## API 请求捕获

1.0.101 的生产流量不再经过保留在包内的 `ExchangeLogger`。插件改为
hook 活跃的 `MainSceneExchangeEventListener.BeforeRequest` / 成功响应入口
（RVA `0x03A46430` / `0x03A46500`），在
`Documents/jbrfz_capture.log` 记录通用 RPC 请求、响应和错误。公会接口被
列为高价值数据，请求/响应 protobuf 同时写入 `Documents/jbrfz_capture/`。
日志时间使用设备本地时区。

## Build

```sh
THEOS=/opt/theos make package
```

Install on the device forwarded to local port 2224:

```sh
THEOS=/opt/theos THEOS_DEVICE_IP=127.0.0.1 THEOS_DEVICE_PORT=2224 \
  make package install
```
