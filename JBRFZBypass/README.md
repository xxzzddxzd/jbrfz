# JBRFZ Bypass

Rootless Theos tweak for `com.devsisters.cc` (CookieRun: Crumble).

## AppSealing

The app's `AppSealingSDK` starts a background integrity loop and treats a
non-zero result from its exported environment checks as an abnormal device.
The tweak resolves those exports from `UnityFramework` at runtime and makes
them report the normal value (`0`). For app version 1.1.001 (2026081018), it also
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
`IsAdRemoveActive` at RVA `0x03DD44A0` and always returns `true`.

This is a client-side gate only. Server-owned inventory/boost records and
anti-cheat validation are unchanged.


## 错误弹框不回登录

业务 RPC 失败由 `MainSceneExchangeEventListener.AfterResponse` 弹出
系统提示框（默认文案 `ErrorCodeUnclassifiedErrorStr`，即
“处理请求过程中出现错误”）。点确定后进入
`AfterError`：未识别错误码或 `AfterActionType.Restart` 会调用
`GameSceneLoader.RestartApp()` 重载登录场景。

tweak 将 `AfterError`（RVA `0x03A85C10`）置空：弹框仍可关闭，
不再强制返回登录页。背景业务可继续跑。


## 序列任务自动执行

序列任务由 `GuidePresenter` 驱动。条件完成后自动领取；未完成且在已验证
清单中的任务，会沿游戏原生入口执行烤箱、十连或开箱动作。未列入清单的
序列任务保持手动。

“消灭 2000 敌人”不会自动刷怪。插件遇到该任务时会立即尝试领取；此后只要
任务仍为当前序列任务，就每 60 秒再尝试一次。该路径不调用游戏按钮中的
`UniTask.Forget()`，服务器拒绝重复领取时只解除加载和等待状态，不进入未观察异常路径。



## 重复任务击杀计数假完成（客户端）

`RequirementUpdaterBase.Register` 会在 `RequirementRecordSet` 里 Remove+Add 新
record，但不会对旧对象调用 `RemoveRecordByType`，`RecordsByType` 会残留幽灵
条目。type `130`（重复 guide 击杀）在 `unitCounts` 缺失时还会走
`CalculateCurrentValue` 的 NotImplemented 异常分支，导致旧记录无法被替换，
UI 可能提前显示 completed，点领取被服务器拒绝。

tweak（始终生效，不依赖自动任务开关）：

- hook `Register`（RVA `0x03E64F28`）：注册前若已有同 id record，先调
  `RemoveRecordByType`（`0x03E64AFC`）清 `RecordsByType`
- hook `CalculateCurrentValue`（RVA `0x03E694C8`）：type `129`/`130` 返回 0，
  避免注册失败

领取请求仍只带 guideId/类型，服务器独立校验；本补丁只修客户端显示与本地计数一致性。

## API 请求捕获

1.1.001 的生产流量不再经过保留在包内的 `ExchangeLogger`。插件改为
hook 活跃的 `MainSceneExchangeEventListener.BeforeRequest` / 成功响应入口
（RVA `0x03A855EC` / `0x03A856BC`），在
`Documents/jbrfz_capture.log` 记录通用 RPC 请求、响应和错误。公会接口被
列为高价值数据，请求/响应 protobuf 同时写入 `Documents/jbrfz_capture/`。
日志时间使用设备本地时区。

## 活动任务

插件不再 Hook 或自动执行“巧克力巨大滴露转盘”等活动任务；活动清单、领取
及“前往”按钮均保持游戏原生手动行为。“自动功能”开关只控制序列任务。

## Build

```sh
THEOS=/opt/theos make package
```

Install on the device forwarded to local port 2224:

```sh
THEOS=/opt/theos THEOS_DEVICE_IP=127.0.0.1 THEOS_DEVICE_PORT=2224 \
  make package install
```
