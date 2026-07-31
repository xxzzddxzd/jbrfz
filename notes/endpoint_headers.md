# Game Endpoint & gRPC Headers (RE)

> Build: CookieRun Crumble / UnityFramework (m17 client)
> Status: keys + default host confirmed from IL2CPP + Flan; live capture hooks added in 0.3.22

## 1. Game gRPC Endpoint

### Default (hardcoded in `Crumble.EndPointConfig`)
- Protocol: `https://`
- Domain: `cc-dev-m17-cc-gameserver-client.dev.devsisters.cloud`
- Port: `443`
- Default (binary fallback): **`https://cc-dev-m17-cc-gameserver-client.dev.devsisters.cloud:443`**

**Live (captured):** **`https://cc-gameserver-client.live.prod.devslime.cloud:443`**

### Live selection order (`CreateNetworkServicesTask`)
1. `EndPointConfig.GetOverrideEndpoint()`  
   - set by `ServerListProvider.SelectServer` (dev server picker) or title console
   - prefs key: `EndPointConfig_overrideEndpoint`
2. Else provisioning `MetadataResponse.game_endpoints` / `ServerEndpoints.game`  
   (native ProvisioningLoader → `FetchServerEndpoints`)
3. Fallback: `EndPointConfig.GetDefault()` above

### Flan control-plane (dev server list, optional)
- Auth: `POST https://api.flan.devsisters.cloud/auth/token/api-key`  
  body:
  ```json
  {"keyId":"720fe118-c343-4a75-944c-12d452a69caa","keySecret":"flan_f50lPiVb3keX2dN2Dk554dNRw03"}
  ```
- Namespaces: `GET https://api.flan.devsisters.cloud/cc/namespaces`  
  header: `Authorization: Bearer <token>`
- For namespace `cc-dev-m17`, metadata link `host-link-for-unity` =
  `cc-dev-m17-cc-gameserver-client.dev.devsisters.cloud`  
  (matches binary default; lastDeploy ~ 2026-07-31)

### Channel
- `GrpcChannelProvider.CreateChannel` → `GrpcChannel.ForAddress(endpoint.ToString())`
- Transport: Grpc.Net.Client + YetAnotherHttpHandler + SslCredentials (HTTP/2)

### Service paths (examples)
- Adventure: `/cc.public.game.AdventureService/StartStage`
- Adventure: `/cc.public.game.AdventureService/CompleteStage`
- Event: `/cc.public.game.EventService/RegisterFriendInviter` (confirm package when binding)
- Package prefix observed: `cc.public.game.<Service>`

## 2. Request Metadata (headers)

Built by `Crumble.MetadataProvider.CreateHeaders(requestId, requestTime, options)`.

| Key | Source | Notes |
|-----|--------|--------|
| **crumble-user-id** | Mid (`CreateMidEntry` / common header[0]) | player MID string, e.g. `ZXPKR2155` |
| **crumble-resource-key** | `GetResourceKey()` / common header[1] | default **`dev-0000000000`** until set from provisioning |
| **application-context-bin** | `ApplicationContextProvider.Get()` protobuf bytes | binary metadata (`-bin` suffix) |
| **request-time** | `UtcTime` of request | string form of server/client UTC time |
| **crumble-request-id** | `Guid.ToString()` | per-request UUID |
| **crumble-access-token** | `LoginManager.session` → **game_access_token** JWT | only when logged in |
| **dev-play-fgs-id** | `LogField.fgsId` | device/analytics id |
| **dev-play-anonymous-id** | `LogField.anonymousId` | |
| **dev-play-app-installed-id** | `LogField.appInstalledId` | |
| **dev-play-devsisters-id** | `LogField.devsistersId` | |
| **dev-play-semi-device-id** | `LogField.semiDeviceId` | |
| **grpc-internal-encoding-request** | optional | when `SenderOption` has bit `0x4` |

Response-related (not request): `crumble-error-code`.

Constant: `MetadataUtils.AppContextKey = "application-context-bin"`.

### application-context-bin payload
Protobuf `cc.public.game.ApplicationContext`:
- `userContext` (field 1): `UserContext`  
  - app / localeOnGame / location / os / timezone / platform[] / device
- `voiceLocaleOnGame` (field 2)

Can be empty-ish for early experiments; real client fills device/locale.

### Access token
From DevPlay `LoginResponse.game_access_token` (JWT):
```json
{"aud":"cc","iss":"ovencloud","sub":"<MID>", ...}
```
Also captured: `refresh_token`, `oven_access_token`, `guest_secret`, `device_secret`.

## 3. Account / guest (pre-game)
- Hosts in binary: `account.prod|stg|sandbox.devsisters.cloud` (+ `.systems`)
- Paths seen: `/auth/v2/account`, `/auth/v2/login-try`, `/auth/v2/login-apple`, ...
- Guest identity on device: mid + `guest_secret` (Keychain) → re-login yields new `game_access_token`

## 4. Minimal offline bot checklist
1. Guest login / reuse mid+guest_secret → get `game_access_token`
2. Open gRPC channel to  
   `https://cc-dev-m17-cc-gameserver-client.dev.devsisters.cloud:443`
3. On every call attach at least:
   - `crumble-user-id: <MID>`
   - `crumble-resource-key: dev-0000000000` (or live key if captured)
   - `crumble-access-token: <game_access_token>`
   - `crumble-request-id: <uuid>`
   - `request-time: <utc>`
   - `application-context-bin: <bytes>` (can start with minimal pb)
4. Stage flow: `StartStage` → battle → `CompleteStage` with reports


## Live capture (ZXPKR2155, 0.3.22, after stage ~1-7/1-8)

```
ENDPOINT_URL = https://cc-gameserver-client.live.prod.devslime.cloud:443
```

**Not** the m17 default. Provisioning overrides to prod-slime live host.

### Real headers sample
```
crumble-user-id=ZXPKR2155
crumble-resource-key=game-data-8319a6-a64b0c
application-context-bin=<binary Entry, not string>
request-time=1785483995178          # UtcTime millis integer string
crumble-request-id=<uuid>
crumble-access-token=<game_access_token JWT aud=cc sub=ZXPKR2155>
dev-play-fgs-id=dd8e88b8-d9c0-4be4-bd07-add9255d2c12
dev-play-anonymous-id=20eb7ac1-7c90-43e4-a3e6-cd2b5e4e1921
dev-play-app-installed-id=BB7E54A7-2995-422E-9697-A4EB655E57DA
dev-play-devsisters-id=117C42DA-1801-46CC-BE20-54BD5715B732
dev-play-semi-device-id=B4D18487-C13B-4760-B4CE-267EC387BB6C
```

### Stage progress observed
- CompleteStage stage=7 startPoint=0/1 (1-7)
- CompleteStage stage=8 startPoint=0/1 (1-8)
- Start/Complete stage=9 startPoint=0 already fired
- Samples under `capture_pull/stage78/`

## 5. Capture (tweak ≥ 0.3.22)
Look in `Documents/jbrfz_capture.log` after relaunch:
- `[CAPTURE][ENDPOINT_URL]`
- `[CAPTURE][CREATE_CHANNEL]`
- `[CAPTURE][HEADERS]`
- existing LOGIN / GUEST_SECRET / StartStage / CompleteStage

## 6. Charles capture 2026-07-31 16:12 (guest create)

File: `~/Downloads/iPhone 2026_7_31 16_12.chlsj`

**Only auth-relevant COMPLETE request:**
- `#12 POST https://account.devplay.com/v3/login`
- Headers: `X-API-Key=wUsUkXPVSujBcOt4mDJX`, `X-Env=prod`, `X-Bundle-Id=com.devsisters.cc`, base64 `X-Os-*` / locale / timezone / app version
- Body: `guest_secret:""` + `lc{fgs_id,anonymous_id,...}` + `device_id`
- Response `code=20000`: mid `HGQGG3371`, `game_access_token`, `refresh_token`, `guest_secret`

Implemented in `iosver/crumble_bot/auth.py` (`guest_login` / `create-guest`).
Verified new mid `YMLSY4865` → invite GNWPX5251 → clear 1-30 ok.
