# CookieRunCrumble 1.0.101 内嵌补丁 IPA

该构建把 `JBRFZBypass` 作为自包含 dylib 注入游戏主程序，并静态链接 Dobby。
成品不依赖设备上的 Substrate/ElleKit，也不会编译请求、响应、登录凭据或 protobuf
采集 Hook。

包含的 1.0.101 固定补丁：

- AppSealing 环境检查兼容与状态修复；
- 打包时静态关闭主程序及 UnityFramework 的 20 个保护线程入口；
- 免广告路径、RPC 错误 loading 修复；
- 任务自动领取、烤箱/十连/宝箱动作及需求记录修复；
- 浮动面板；
- 通过该版本导出的 `il2cpp_resolve_icall` 解析 `UnityEngine.Time::set_timeScale(System.Single)`，面板可切换 3×/1×；开启后每 500 ms 重申一次倍率。

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

## 通行证 UI 诊断补丁

`patch_pass_ui_diagnostic.py` 仅用于 1.0.101 的客户端显示验证：它让高级奖励格按
可用状态渲染，同时禁用高级奖励点击处理，因此不会提交高级奖励领取 RPC，也不会
修改登录数据中的服务端权益字段。

```bash
python3 ipa_patch/patch_pass_ui_diagnostic.py /path/to/UnityFramework
python3 ipa_patch/patch_pass_ui_diagnostic.py --restore /path/to/UnityFramework
```

脚本会校验两处原始指令；版本或二进制不匹配时直接拒绝修改。修改安装副本后需要
重新签名对应 framework 和 app。
