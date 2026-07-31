# 好友邀请 / 过关自动化 - 采集说明

## 游戏内链路（已反编译确认）

1. **邀请人**
   - `DeepLinkManagerWrapper.CreateFriendInvitationLink(Mid)`
   - OneLink: `https://crumble.onelink.me/cfu9/gh3vzc2g`
   - 参数：`deep_link_action=invite`，`deep_link_value` 含 `/invite` + `mid=<邀请人MID>`

2. **被邀请人**
   - 深链解析得到 inviter mid
   - `EventApi.RegisterFriendInviterAsync(inviterId)` → gRPC `EventService/RegisterFriendInviter`
   - Request: `RegisterFriendInviterRequest { inviter_id: string }`

3. **过关条件**
   - `GameSetting.RequiredClearStageForInviteFriends`（表字段，疑似 30）
   - 需求类型 `RequirementType.InviteFriends = 97`
   - 被邀请人通关后，邀请人 `FriendInvitation.completed_invitee_ids` 增加

4. **建号**
   - DevPlay `LoginWithGuest` → `GuestLoginInfo { mid, guestSecret }`（Keychain）
   - LoginResponse: `game_access_token` / `refresh_token` / `guest_secret` / `token`
   - 游戏服 `CrumbleService/SignUp`

5. **推图**
   - `StageApi.StartStageAsync(stageIndex, team, startPoint, trigger, report)`
   - `StageApi.CompleteStageAsync(stageIndex, startPoint, StageClearReport, ClientBattleReport)`
   - `StageClearReport`: randomSeed, battleTime, battleTeamReport

## 0.3.17 Capture 输出

设备沙盒：
- `Documents/jbrfz_capture.log` 文本日志
- `Documents/jbrfz_capture/*.bin` 高价值 protobuf 二进制
- 原有 `Documents/jbrfzbypass.log`

标签：
- `[CAPTURE][ENDPOINT]` 游戏服地址
- `[CAPTURE][GUEST_SECRET]` 游客 mid/secret
- `[CAPTURE][LOGIN]` token 等
- `[CAPTURE][INVITE_LINK]` 邀请链接
- `[CAPTURE][REQ/OK/ERR]` 过滤后的 gRPC（Stage/SignUp/Invite/...）

## 请你配合采集的操作

1. 杀掉重开游戏（已装 0.3.17）
2. 正常进主界面（会采到 endpoint / login / guest）
3. 打开好友邀请活动页，点分享/复制邀请链接
4. 打通至少 **1 关** 主线（采到 StartStage + CompleteStage 完整包）
5. 如方便：用小号打开邀请链接并绑定（采到 RegisterFriendInviter）

完成后告诉我，我从设备拉日志并开始写 Python 建号+过关脚本。


## RegisterFriendInviter live sample (2026-07-31)
- invitee mid: `ZXPKR2155`
- inviter_id: `GNWPX5251`
- log: `[CAPTURE][RegisterFriendInviter] inviter=GNWPX5251`
- endpoint: `https://cc-gameserver-client.live.prod.devslime.cloud:443`
