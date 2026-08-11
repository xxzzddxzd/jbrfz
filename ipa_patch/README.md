# CookieRunCrumble 1.0.101 内嵌补丁 IPA

该构建把 `JBRFZBypass` 作为自包含 dylib 注入游戏主程序，并静态链接 Dobby。
成品不依赖设备上的 Substrate/ElleKit，也不会编译请求、响应、登录凭据或 protobuf
采集 Hook。

包含的 1.0.101 固定补丁：

- AppSealing 环境检查兼容与状态修复；
- 免广告路径、RPC 错误 loading 修复；
- 任务自动领取、烤箱/十连/宝箱动作及需求记录修复；
- 浮动面板；
- `UnityEngine.Time.set_timeScale` 固定地址 `UnityFramework + 0x0BB38760`，面板可切换 3×/1×。

默认构建并做 ad-hoc 签名（适合 TrollStore/已允许自签包的设备）：

```bash
./ipa_patch/build_ipa.sh
```

使用 Apple Development 描述文件签名：

```bash
SIGN_IDENTITY='Apple Development: ...' \
MOBILEPROVISION=/absolute/path/profile.mobileprovision \
./ipa_patch/build_ipa.sh
```

可通过 `SOURCE_IPA`、`MAIN_BINARY`、`UNITY_BINARY`、`OUTPUT_IPA` 和
`DOBBY_LIB` 覆盖默认路径。构建会校验游戏版本、外部 Hook 依赖以及请求采集特征串。
