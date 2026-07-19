#!/usr/bin/env python3
"""Hermes Agent Release Script

Generates changelogs and creates GitHub releases with CalVer tags.

Usage:
    # Preview changelog (dry run)
    python scripts/release.py

    # Preview with semver bump
    python scripts/release.py --bump minor

    # Create the release
    python scripts/release.py --bump minor --publish

    # First release (no previous tag)
    python scripts/release.py --bump minor --publish --first-release

    # Override CalVer date (e.g. for a belated release)
    python scripts/release.py --bump minor --publish --date 2026.3.15
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "hermes_cli" / "__init__.py"
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"

# ACP Registry manifest must stay version-locked with pyproject.toml.
# tests/acp/test_registry_manifest.py enforces this lockstep so the release
# bump touches both files atomically.
ACP_REGISTRY_MANIFEST = REPO_ROOT / "acp_registry" / "agent.json"

# ──────────────────────────────────────────────────────────────────────
# Git email → GitHub username mapping
# ──────────────────────────────────────────────────────────────────────

# FROZEN legacy mappings — do NOT add new entries here. New contributor
# mappings live as one-file-per-email entries under contributors/emails/
# (see contributors/README.md), which merge-conflict-free by construction.
# This dict is kept only so existing history keeps resolving; the effective
# AUTHOR_MAP below merges it with the directory (directory wins).
LEGACY_AUTHOR_MAP = {
    "122438640+ragingbulld@users.noreply.github.com": "ragingbulld",  # PR #65606 salvage (non-finite API wait deadlines; #65746)
    "zzpigpinggai@users.noreply.github.com": "zzpigpinggai",  # PR #66017 salvage of #63617 (OpenRouter explicit-provider picker visibility)
    "stellarisw@users.noreply.github.com": "StellarisW",  # PR #66222 salvage (Discord WebSocket liveness + systemd watchdog; #26656 follow-up)
    "wx.xw@bytedance.com": "wxy-nlp",  # PR #66222 salvage (systemd event-loop watchdog co-author)
    "sam7894604@gmail.com": "sam7894604",  # PR #55803 salvage (discord: /reasoning slash choices)
    "bryan@users.noreply.github.com": "hydraxman",  # PR #62028 salvage (copilot xhigh) — regression-test commit authored under a bare-noreply local git identity; PR author is @hydraxman
    "antydizajn@gmail.com": "antydizajn",  # PR #36043 salvage (auxiliary: route custom:<name> through named-provider arm + Palantir Bearer auth)
    "252620095+briandevans@users.noreply.github.com": "briandevans",  # PR #64951 salvage (lmstudio: clamp max/ultra reasoning effort)
    "kar.iskakov@gmail.com": "karfly",  # PR #64012 salvage (gateway: surface extended reasoning efforts)
    "enzo.eliott.adami@gmail.com": "enzo-adami",  # PR #66637 salvage (compression: preserve human intent and durable handoffs)
    "kimyeon30@naver.com": "rlaehddus302",  # PR #61985 salvage (gateway: secondary-adapter auth callback profile)
    "burke@autreymail.com": "bautrey",  # PR #66479 salvage (gateway reliability hardening: Bedrock liveness, supervised watchers, launchd respawn throttle)
    "agungsubastian1963@gmail.com": "aguung",  # PR #64461 salvage (gateway: multiplex secret_scope for authz/Slack/webhooks)
    "jtstothard@gmail.com": "jtstothard",  # PR #63256 salvage (gateway: multiplex secondary adapter config validation)
    "fjlaowan@proton.me": "fjlaowan1983",  # PR #11256 salvage (honcho: reject whitespace-only reasoning queries)
    "RainbowAndSun@users.noreply.github.com": "RainbowAndSun",  # PR #62982 salvage (honcho: observer target in prefetch context)
    "pi@hermes.local": "Elektrofussel",  # PR #61675 salvage (honcho: defaultHost + private-range local URL detection)
    "doogie@spark.local": "SAMBAS123",  # PR #64986 salvage (gateway: multiplex primary bot token scope)
    "maartendormenatteysen@hotmail.com": "MaartenDMT",  # PR #65637 salvage (gateway: retry failed transcript appends and rebuild corrupted FTS)
    "emrekoca2003@gmail.com": "kocaemre",  # PR #36051 salvage (docs: audit round 3 code/doc reconciliation)
    "focusedmiqa@gmail.com": "m1qaweb",  # PR #29290 salvage (gateway: strip /queue prefix when idle)
    "13574+otsune@users.noreply.github.com": "otsune",  # PR #36019 salvage (kanban: attachment toolset + CLI)
    "205466933+wesleion@users.noreply.github.com": "wesleion",  # PR #36049 salvage (telegram: per-topic free-response allowlist)
    "evefromwayback@gmail.com": "evefromwayback",  # PR #64611 salvage (agent: never load install-tree AGENTS.md as project context)
    "Regina@Andreys-Mini.true.true": "Rival",  # PR #64935/#64936 salvage (state: restore-boundary alternation repair; agent: turn-overlap tripwire)
    "embwl0x@users.noreply.github.com": "embwl0x",  # PR #65105 salvage (gateway: preserve external supervisor ownership)
    "41409874+275****8943@users.noreply.github.com": "2751738943",  # PR #54785 salvage (tui: post-turn completion ownership routing)
    "Burgunthy@users.noreply.github.com": "Burgunthy",  # PR #20096 salvage (gateway: profile-based routing for inbound messages)
    "75556242+webtecnica@users.noreply.github.com": "webtecnica",  # PR #63360 salvage (nous: restore inference-api base_url)
    "skosarevivan@yandex.ru": "Epoxidex",  # PR #29820 salvage (ollama: top-level reasoning_effort=none; #25758)
    "jdjiayou@163.com": "JiaDe-Wu",  # PR #34742 salvage (bedrock: bearer routing + streaming fallback + image decode; #28156)
    "changhyun.min@gmail.com": "minchang",  # PR #42231 salvage (providers: add Upstage Solar)
    "neo@neodeMac-mini.local": "neo-claw-bot",  # PR #58465 salvage (moa: drop empty user turns from advisory view)
    "2024104039@mails.szu.edu.cn": "pixel4039",  # PR #64420 salvage (streaming: retry zero-chunk streams)
    "marceloparra.hm@gmail.com": "marcelohildebrand",  # PR #42346 salvage (lmstudio: JIT load mode)
    "qlskssk@gmail.com": "Soju06",  # agent turn-latency perf PRs
    "m.guttmann@journaway.com": "mguttmann",  # PR #63738 salvage (Anthropic setup-token pool auth normalization)
    "VrtxOmega@pm.me": "VrtxOmega",  # PR #43809 salvage (desktop: WSL folder-picker path bridge)
    "gn00742754@gmail.com": "SemonCat",  # PR #56786 salvage (Slack Agent View manifests and Assistant APIs)
    "KCAYAAI@users.noreply.github.com": "KCAYAAI",  # PR #62248 partial salvage (resume typing after clarify reply)
    "jake.long.vu@vucar.net": "jakelongvu-bot",  # PR #36683 partial salvage (approval: honor canonical approvals.timeout in gateway waits)
    "luigi@users.noreply.github.com": "Tortugasaur",  # PR #43205 salvage (desktop: profile-aware three-way approval mode statusbar control)
    "kavi@local.hermes": "kavioavio",  # Issue #46544 / PR #47705 evolution (smart DENY exact-operation owner override)
    "135129512+ansel-f@users.noreply.github.com": "ansel-f",  # PR #62388 salvage (approval: allow exact verifier temp cleanup without broadening rm safety boundary)
    "robert@modern-minds.ai": "Hopfensaft",  # PR #31933 salvage (dashboard: align approvals.mode dropdown with canonical engine values)
    "palmer@dugoutfantasy.com": "professorpalmer",  # PR #48591 salvage (sessions: CLI workspace filter + restore-cwd-on-resume)
    "true@supersynergy.de": "Supersynergy",  # PR #59241 salvage (desktop: workspace path status-bar action)
    "me@roryford.com": "roryford",  # PR #63132 salvage (compression: fail closed for errors from a resolved lock API, preserving lineage)
    "esthon@gmail.com": "esthonjr",  # PR #61950 salvage (desktop: legacy non-git workspace grouping + Windows path identity)
    "drexux0@gmail.com": "Drexuxux",  # PR #36042 salvage (gateway: /footer reachable mid-run via safe-toggle set)
    "iganapolsky@gmail.com": "IgorGanapolsky",  # PR #62125 salvage (compaction anti-thrash threshold verification)
    "275853971+aeyeopsdev@users.noreply.github.com": "aeyeopsdev",  # PRs #36035/#36068 salvage (google-chat: http inbound without pubsub; clarify cards)
    "tturney1@gmail.com": "TheTom",  # PR #62696 salvage (gateway: expand @ context references under runtime/session model resolution)
    "1822947159@qq.com": "ljy-2000",  # PR #62204 adopted in #62290
    "xwolf.live@gmail.com": "vizi0uz",  # PR #59795 adopted in #62290
    "wilsonkinyuam@gmail.com": "WilsonKinyua",  # PR #62052 (tui: persist unflushed conversations on disconnect/restart)
    "humphreysun98@gmail.com": "HumphreySun98",  # PR #61142 salvage (web: null web/backend config value guards)
    "sonxi@nous.local": "17324393074",  # PR #53196 salvage (tools_config: known_plugin_toolsets null guard; commit under unlinked local identity)
    "lemonwan@users.noreply.github.com": "lemonwan",  # PR #59430 sibling salvage (adapter reconnect contract guard)
    "luxuguangno1@163.com": "luxuguang-leo",  # PR #52966 + #52908 salvage (QQBot reconnect + Feishu Channel signaling)
    "grace@weeb.onl": "evelynburger",  # PR #57544 salvage (gateway: webhook payload filters + route scripts; commit under unlinked identity)
    "contato@siteup.com.br": "SiteupAgencia",  # PR #57435 salvage (tui_gateway: back off notification poller when session is busy; #55578)
    "164521089+rainbowgits@users.noreply.github.com": "rainbowgore",  # PR #59405 salvage (mcp: bound stdio initialize handshake to stop subprocess/FD leak; #59349)
    "sage@Sages-Mac-mini.local": "thestudionorth",  # PR #60015 salvage (mcp: parent-death watchdog for stdio children; commit under unlinked local identity)
    "4087127+vampyren@users.noreply.github.com": "vampyren",  # PR #59830 salvage (kanban: grab-to-pan board scrolling; original commit under unlinked local identity)
    "spiky02plateau@users.noreply.github.com": "spiky02plateau",  # PR #32824 salvage (usage: fetch Codex account limits from the credential pool in pool-only setups; superseded by #60028)
    "taylorhp@gmail.com": "hwrdprkns",  # PR #36896 salvage (secrets: 1Password op:// secret source + shared _cache substrate, adapted onto the SecretSource interface)
    "ishengeqi@163.com": "isheng-eqi",  # PR #59428 salvage (cron: reject past one-shot timestamps in update_job fallback + resume_job; #59395). Also PR #59446 salvage (cron: advance one-shot next_run_at before dispatch so concurrent gateway+desktop schedulers can't double-execute; #59229).
    "derek2000139@qq.com": "derek2000139",  # PR #57838 salvage (desktop/windows: pre-write update marker before quit dwell so the renderer's waitForUpdateToFinish gate parks instead of respawning a backend that re-locks venv .pyd files mid-update)
    "AndreasHiltner@users.noreply.github.com": "AndreasHiltner",  # PR #56854 salvage (gateway: route multiplex profile responses through the profile's own adapter — 53-site _adapter_for_source sweep)
    "AlexFucuson9@users.noreply.github.com": "AlexFucuson9",  # PR #61347 salvage (agent: reapply provider headers after model switch; #61099)
    "allenliang2022@users.noreply.github.com": "allenliang2022",  # PR #56932 test coverage folded into #56909 salvage (408 → retryable timeout)
    "m888.braun@hotmail.com": "ManniBr",  # PR #57417 partial salvage (gateway: fail-closed adapter resolution for unregistered secondary profiles)
    "poowis2011@hotmail.com": "Umi4Life",  # PR #47377 salvage (agent: emit one-shot fallback switch notice on successful fallback so gateway users see model/provider change; #35419)
    "austin@openvm067.space": "austinlaw076",  # PR #57563 partial salvage (auth: lazy per-profile Anthropic OAuth file; gateway: whatsapp_cloud/line added to port-binding platform set)
    "sunsky.lau@gmail.com": "liuhao1024",  # PR #56993 salvage (gateway: process-level HERMES_HOME for pid/lock/status identity files; #56986)
    "roseycomanagement@roseyco.co.uk": "Roseyco-management",  # PR #63581 salvage (telegram: require getUpdates progress before polling is healthy; #63243, #63766)
    "nima20002000@users.noreply.github.com": "nima20002000",  # PR #36022/#36025 salvage (code-exec truncation metadata; achievements hover loop)
    "gauravsaxena.jaipur@gmail.com": "gauravsaxena1997",  # PR #59868 partial salvage (agent: guard response.text against httpx.ResponseNotRead in _summarize_api_error; #59769)
    "blueirobin02@gmail.com": "irresi",  # PR #59048 salvage (gateway: scope reset banners' session info to the serving profile; #59003)
    "jashlee+microsoft@microsoft.com": "s905060",  # PR #57943 salvage (photon: auto-reinstall stale sidecar node_modules when lockfile is newer than npm's install marker; #59169)
    "lohinth25@proton.me": "l0h1nth",  # PR #32210 salvage (mattermost: accept leading-space slash commands from mobile clients; #25184)
    "roseycomanagement@roseyco.co.uk": "arnispiekus",  # PR #63581 salvage (telegram: require getUpdates progress before polling is healthy; #63243)
    "perkintahmaz50@gmail.com": "devatnull",  # PR #58704 salvage (whatsapp: native Baileys polls, clarify-as-poll, location pins, structured quoted replies, PTT/audio split, bridge_helpers extraction)
    "tim@iteachyouai.com": "tjp2021",  # PR #4097 salvage (copilot: per-turn x-initiator header so user prompts bill as premium requests; #3040)
    "39274208+falkoro@users.noreply.github.com": "falkoro",  # PRs #58519/#58520 salvage (config: env-ref-aware load_config cache invalidation; auxiliary: honor auxiliary.<task>.base_url/api_key with explicit provider arg)
    "3723267+kevinrajaram@users.noreply.github.com": "kevinrajaram",  # PR #3850 salvage (gateway: add POSIX system dirs to PATH so launchctl/systemctl resolve under UV's minimal-PATH bundled Python; #3849)
    "lord-dubious@users.noreply.github.com": "lord-dubious",  # PR #58453 salvage (preserve static custom provider models declared as dict rows)
    "williamumu@users.noreply.github.com": "williamumu",  # PR #31041 salvage (pairing: merge split legacy/new pairing store dirs at PairingStore init so approved users aren't re-prompted to pair)
    "jonathan@mintrx.com": "JAlmanzarMint",  # PR #52688 salvage (vision: rasterize SVG / re-encode unsupported raster formats to PNG before embedding), folded into #57890
    "alex@fireworks.ai": "alex-fireworks",  # PR #61182 salvage (Fireworks AI model-provider integration)
    "al3060388206@gmail.com": "ooiuuii",  # PR #58466/#58377 salvage (redact: fireworks fw-/fpk_ prefixes; telegram: redact bot tokens out of transport error strings). Also PR #58433 salvage (codex: accept recorded final_text when app-server omits turn/completed) and PR #58472 salvage (gateway: cap proxy SSE line buffer at 16MiB).
    "Jigoooo@users.noreply.github.com": "Jigoooo",  # PR #58474 salvage (auxiliary: fall back to token resolver when anthropic pool has no usable entry)
    "root@vmi3351581.contaboserver.net": "ostravajih",  # PR #58374 salvage (poolside: coerce integer finish_reason and tool_call id to strings)
    "hello@sahil-shubham.in": "sahil-shubham",  # PR #58448 salvage (whatsapp_cloud: honor documented WHATSAPP_CLOUD_ALLOWED_USERS / ALLOW_ALL_USERS in the DM intake gate)
    "ahmet.tunc@gmail.com": "Ahmett101",  # PR #58445 salvage (profiles: allowlist default-export roots + preserve symlinks)
    "ignaciopastorsan@gmail.com": "IpastorSan",  # PR #63690 salvage (codex: rescue reasoning-only turns that die after 3 continuation attempts)
    "Ahmett101@users.noreply.github.com": "Ahmett101",  # PR #59455 salvage (background-review: guard summarize against list-shaped tool responses; #59437)
    "wyuebei@gmail.com": "wyuebei-cloud",  # PR #56640 salvage (hermes journey: replace GNU-only %-d strftime with dt.day for Windows)
    "yingwaizhiying@gmail.com": "msh01",  # PR #58250 salvage (telegram: wall-clock init timeout via daemon-thread deadline + abandon the shielded initialize task on timeout so the retry ladder advances instead of hanging on attempt 1/8 under s6 supervision; #58236). Also covers PR #58276 salvage (compression: preserve a real user turn after compaction; #55677).
    "danilo@falcao.org": "danilofalcao",  # PR #56674 salvage (update: skip unsupported platform.matrix lazy refresh on native Windows — python-olm has no Windows wheel)
    "huanshan5195@users.noreply.github.com": "huanshan5195",  # PR #57601 salvage (custom-provider: emit reasoning_effort at the live CustomProfile path so GLM-5.2/ARK/vLLM/Ollama endpoints receive it; + "max" reasoning level)
    "infinitycrew39@gmail.com": "infinitycrew39",  # PR #56431 salvage (honor live vLLM context limits on local endpoints)
    "jonathan.kovacs999@gmail.com": "CocaKova",  # PR #57692 salvage (cron: run jobs under the profile secret scope so get_secret does not fail-close with UnscopedSecretError under profile isolation)
    "hermes.wanderer@yahoo.com": "trismegistus-wanderer",  # PR #31856 salvage (gateway: defer idle-TTL agent-cache eviction until the session store says the session actually expired, so the expiry watcher can still fire MemoryProvider.on_session_end with the live transcript; #11205)
    "louis@letsfive.io": "Mibayy",  # PR #3243 salvage (/compact alias + preview/aggressive flags for /compress)
    "louis@letsfive.io": "Mibayy",  # PR #3176 salvage (api-server: per-client model routing via model_routes)
    "jneeee@outlook.com": "jneeee",  # PR #3526 salvage (extra HTTP headers for LLM API calls via config.yaml)
    "ai-lab@foxmail.com": "CrazyBoyM",  # PR #55828 salvage (image_gen openai-codex: wire image-to-image / reference-image editing via Codex Responses input_image parts; magic-byte + read-guard + 25MB-cap + clamp-to-16 hardening)
    "r0gersm1th@users.noreply.github.com": "r0gersm1th",  # PR #3219 salvage (whatsapp bridge: resolve LID sender IDs to phone numbers in the message payload so phone-based allowlists match; commit authored by collaborator r0gersm1th, PR by @ajmeese7)
    "louis@letsfive.io": "Mibayy",  # PR #3296 salvage (status: provider label honors config.yaml model.base_url, not just OPENAI_BASE_URL env)
    "me@keslerm.com": "keslerm",  # PR #3459 salvage (gateway: 'log' tool_progress mode — silent in chat, tool calls appended to ~/.hermes/logs/tool_calls.log via rotating handler; duplicate of #3458 by @dlkakbs who submitted 4 min earlier — both credited)
    "david.d.zhang@gmail.com": "Git-on-my-level",  # PR #3659 salvage (gateway: persist per-session /model overrides across gateway restarts)
    "tarunravi@gmail.com": "tarunravi",  # PR #2696 salvage (api-server: inline MEDIA:<path> image tags as base64 data URLs in final responses so remote OpenAI-compatible frontends can render server-local screenshots; the PR's tool-progress-streaming and SSE-sentinel pieces were independently superseded on main)
    "aqdrgg19@gmail.com": "VolodymyrBg",  # PR #2861 salvage (webhook: drop the unused full request payload from retained _delivery_info entries — up to ~1MB dead weight per delivery for the 1h idempotency TTL)
    "ohyes9711@gmail.com": "CharmingGroot",  # PR #2794 salvage (email: guard msg_data[0][1] against malformed IMAP fetch structures so one bad response can't abort the batch and permanently lose seen-marked messages; Message-ID domain falls back to localhost when EMAIL_ADDRESS lacks '@')
    "sahibzada@fastino.ai": "sahibzada-allahyar",  # PR #39227 salvage (desktop: configured terminal.cwd overrides a stale remembered workspace-cwd localStorage value when no session is active; #38855)
    "ronaldrj@gmail.com": "rarf",  # PR #56966 salvage (desktop chat model picker: hide implicitly discovered providers unless explicitly configured)
    "jvsantos.cunha@gmail.com": "plcunha",  # PR #55300 salvage (gateway: record child gateway peer metadata after a compression session-id rotation and repoint stale sessions.json compression-parent entries to the recovered live child; consolidated in the compression-routing-integrity salvage)
    "jakepresent1@gmail.com": "jakepresent",  # PR #55721 salvage (gateway: identity-guard stale in-flight compression splits — a late run may publish its compressed child only if its run generation is still current and the session key still points at the run's original parent, so an old run can't overwrite a newer /new or moved binding)
    "gumclaw@gumroad.com": "gumclaw",  # PR #57322 salvage (gateway: close per-delivery webhook sessions on completion so prune_sessions can reap them — fixes unbounded state.db growth from unprunable ended_at=NULL webhook rows)
    "zhangml@tech.icbc.com.cn": "zmlgit",  # PR #54872 salvage (multiplex-profile kanban: route task notifications via the owning profile's adapter + wake the creator agent with a synthetic internal MessageEvent on terminal events)
    "1079826437@qq.com": "nankingjing",  # PR #56404 salvage (gateway: while a state.db compression lock is held for the session, demote busy_input_mode 'interrupt' to 'queue' so a rapid message burst can't interrupt and fork orphaned compression siblings off a stale parent; #56391)
    "ud@arubangles.com": "udatny",  # PR #29433 salvage (subdirectory_hints: catch RuntimeError from Path.expanduser()/Path.home() so a literal ~ in tool-call args — e.g. LLM "~500-700" or ~unknownuser — can't escape the hint walker and crash the conversation loop)
    "brett@personalfinancelab.com": "brett539",  # PR #49369 salvage (cap Telegram initialize() with asyncio.wait_for(HERMES_TELEGRAM_INIT_TIMEOUT, default 30s) per attempt so an unreachable fallback-IP connect chain can't block gateway startup indefinitely; add WARNING progress logs before DoH discovery and each connect attempt)
    "randomuser2026x@proton.me": "randomuser2026x",  # PR #50204 salvage (gateway /restart under systemd: probe both system + --user scope for MainPID instead of hardcoding --user; always exit 75 so RestartForceExitStatus=75 revives the unit under Restart=on-failure too, not just Restart=always)
    "mac-studio@Fabios-Mac-Studio.local": "valenteff",  # PR #53277 salvage (macOS launchd reload: retry bootstrap via _launchctl_bootstrap until launchctl-list confirms registration or the restart-drain window elapses; retry TimeoutExpired not just CalledProcessError; log persistent orphans)
    "steve@lightpathapps.com": "slawt",  # PR #8427 salvage (Google Vertex AI provider for Gemini: OAuth2 token minting via service-account JSON / ADC on the OpenAI-compat endpoint, rewired as a provider profile with per-turn 401 token refresh)
    "gary@bitcryptic.com": "bitcryptic-gw",  # PR #53997 salvage (Matrix E2EE: resolve device_id via query_keys({mxid: []}) when whoami returns none; guard verification call sites so query_keys is never sent [null]; reset _device_id_unverified at connect() start; disconnect before reconnect)
    "gromyko.ss83@gmail.com": "Gromykoss",  # PR #56372 salvage (context_compressor merge-into-tail: place END MARKER last, wrap prior tail content in [PRIOR CONTEXT]...[END OF PRIOR CONTEXT] delimiters so the model doesn't read it as a fresh message)
    "hodlclone@gmail.com": "HODLCLONE",  # PR #49351 salvage (Nous Portal token resilience: rotate refresh tokens write-through to the source auth store in profile mode, skip Nous fallback when no local token, sync gateway session model after fallback)
    "7698789+abchiaravalle@users.noreply.github.com": "abchiaravalle",  # PR #46997 salvage (recover resume_pending sessions: dual freshness signal + empty-turn safety net so restart auto-resume never sends a blank user turn)
    "swissly@users.noreply.github.com": "swissly",  # PR #47167 salvage (wrap cron delivery thread-pool fallback in its own try/except so a per-target failure can't escape the except-RuntimeError block and crash the multi-target delivery loop; #47163)
    "53571168+shawchanshek@users.noreply.github.com": "shawchanshek",  # PR #44126 salvage (strip <think>...</think> reasoning blocks from title-generator LLM output via the canonical strip_think_blocks scrubber so reasoning-model output can't leak into session titles)
    "30854794+YLChen-007@users.noreply.github.com": "YLChen-007",  # PR #27289 salvage (case-insensitive streaming reasoning-tag filter in cli.py _stream_delta + gateway stream_consumer so mixed-case variants like <Think>/<ThInK> are suppressed, not just the hardcoded case literals)
    "27672904+kangsoo-bit@users.noreply.github.com": "kangsoo-bit",  # PR #47508 salvage (keep Telegram gateway alive on transient bootstrap network errors: best-effort deleteWebhook + resilient start_polling degrade to background recovery instead of failing startup)
    "259353979+testingbuddies24@users.noreply.github.com": "testingbuddies24",  # PR #43192 salvage (strip orphan think-tag close tags in progressive gateway stream so a bare </think> whose open was dropped upstream can't leak to the user)
    "shx_929@163.com": "Lazymonter",  # PR #42914 salvage (retry launchd bootstrap after bootout on EIO for install/start instead of degrading to detached)
    "96322396+WXBR@users.noreply.github.com": "WXBR",  # PR #46183 salvage (persist recovered final_response at the finalize_turn chokepoint so recovery-path breaks don't drop the delivered assistant row)
    "dmabry@sparky.fabe-gray.ts.net": "dmabry",  # PR #63862 salvage (output-cap retry: use provider available_tokens + request estimate; exempt parseable vLLM/LM Studio errors from compression-disabled guard)
    "sahil.rakhaiya117814@marwadiuniversity.ac.in": "SahilRakhaiya05",  # PR #44073 salvage (fail-closed gateway/external-surface hardening: own-policy defaults, open-policy startup guard, profile-aware multiplex authz, API-server auth, execute_code per-session RPC token)
    "5848605+itenev@users.noreply.github.com": "itenev",  # PR #22753 salvage (asyncify model-context resolution in gateway message path so blocking requests.get can't starve Discord heartbeats)
    "arthur.zhang@ingenico.com": "arthurzhang",  # PR #34718 salvage (redact Slack App-Level xapp- tokens in agent/redact.py + gateway/run.py)
    "290873280+rrevenanttt@users.noreply.github.com": "rrevenanttt",  # PR #40773 salvage (close hardline rm bypass via quoted paths and ${HOME} brace form)
    "290871358+Vesna-9@users.noreply.github.com": "Vesna-9",  # PR #41274 salvage (collapse shell line continuations before dangerous/hardline pattern matching so `rm -rf \<newline>/` can't bypass the yolo-proof hardline floor)
    "214165399+kernel-t1@users.noreply.github.com": "kernel-t1",  # PR #41349 salvage (.env sanitizer: only split when line starts with a known KEY= and preceding values are plain tokens; keep URL/query/whitespace secrets verbatim)
    "290858493+sasquatch9818@users.noreply.github.com": "sasquatch9818",  # PR #41198 salvage (defang untrusted-tool-result delimiter against tag injection; drop forgeable startswith fast-path)
    "jnibarger01@gmail.com": "jnibarger01",  # PR #35130 salvage (ReDoS-bound threat-pattern filler + FTS5 query cap + V4A Move-File approval/traversal targets)
    "yong2bba@gmail.com": "yong2bba",  # PR #49830 salvage (harden browser tool safety boundaries: config-gated risky-eval blocklist, force-redact browser/CDP/supervisor output, session-ownership tracking, credential-query denylist)
    "info@djimit.nl": "djimit",  # PR #48034 salvage (recover from truncated gateway responses: 4 continuation retries + exponential token headroom + normalize empty partials)
    "lubos@komfi.health": "lubosxyz",  # PR #49225 salvage (persist codex app-server turns to session DB via agent_persisted=False so session_search/distill see gateway conversations)
    "290868363+petrichor-op@users.noreply.github.com": "petrichor-op",  # PR #41281 salvage (never persist ephemeral empty-response recovery scaffolding to the SQLite session store / JSON log; filter by flag not position)
    "283494121+redactdeveloper@users.noreply.github.com": "redactdeveloper",  # PR #36897 salvage (route /sessions & /history through prompt_toolkit-safe print; filter doctor missing-key summary to CLI-enabled toolsets)
    "charleneleong84@gmail.com": "charleneleong-ai",  # PR #11736 salvage (classify Anthropic "out of extra usage" 400 as billing)
    "janrenz@Mac.fritz.box": "janrenz",  # PR #35862 salvage (prompt_caching.enabled escape hatch for strict providers)
    "syahidfrd@gmail.com": "syahidfrd",  # PR #17059 salvage (tag unverified senders in Slack thread context to mitigate indirect prompt injection)
    "22971845+H2KFORGIVEN@users.noreply.github.com": "H2KFORGIVEN",  # PR #22523 salvage (turn-pair preservation: never orphan the last user ask at head_end during compaction)
    "5823452+sgabel@users.noreply.github.com": "sgabel",  # PR #13139 salvage (redact secrets in user-facing approval prompts)
    "130270192+CRWuTJ@users.noreply.github.com": "CRWuTJ",  # PR #17082 salvage (cancel delayed Telegram deliveries on disconnect so buffered flushes don't dispatch into a torn-down session)
    "cyb3rwr3n@users.noreply.github.com": "cyb3rwr3n",  # PR #11333 salvage (sanitize FTS5 queries for natural-language recall in holographic memory)
    "9350182+codexGW@users.noreply.github.com": "codexGW",  # PR #12302 salvage (Discord raw <@!ID> mention detection + drop bare mention-only pings)
    "chufengfan@jackroooc-2.local": "jackroofan",  # PR #54609 salvage (add anthropic to MoA _slot_runtime name-preserve set; OAuth sk-ant-oat* needs Bearer + anthropic-beta header)
    "igor.izotov@gmail.com": "iizotov",  # PR #54912 salvage (add bedrock to MoA _slot_runtime name-preserve set; SigV4-signed client, placeholder aws-sdk api_key)
    "justin@newartifice.com": "JustinOhms",  # PR #24469 salvage (route native-SDK delegation providers through runtime resolver; fail on '(empty)' sentinel instead of accepting it as success)
    "186512915+lEWFkRAD@users.noreply.github.com": "lEWFkRAD",  # PR #53848 salvage (stream the MoA aggregator response to the user)
    "193368749+jimmyjohansson84@users.noreply.github.com": "jimmyjohansson84",  # PR #27123 salvage (Kanban unknown-skill warn-instead-of-crash; #27136)
    "gxalong@gmail.com": "Jeffgithub0029",  # PR #28558 salvage (chunk Telegram text *after* MarkdownV2/HTML formatting so escaping inflation can't push a send over the 4096 UTF-16 limit; #28557)
    "273238055+fayenix@users.noreply.github.com": "fayenix",  # PR #28846 salvage (normalize _cfg_model in gateway fallback-eviction so vendor-prefixed config matches stripped agent.model on native providers)
    "phanvanhoa@gmail.com": "theAgenticBuilder",  # PR #14180 salvage (route delegate_task progress lines through _safe_print so ACP stdio JSON-RPC frames stay clean)
    "huangxudong663@gmail.com": "huangxudong663-sys",  # PR #15157 salvage (isinstance(dict) guard on tool-call model_extra; NVIDIA NIM non-dict crash)
    "39369769+jasonQin6@users.noreply.github.com": "jasonQin6",  # PR #15093 salvage (session staleness guard on stream consumer run() loop; #11016 follow-up)
    "znding04@gmail.com": "znding04",  # PR #15487 salvage (distinguish OpenRouter upstream 429 from account 429; upstream_rate_limit failover reason)
    "zkowkmdx@sharklasers.com": "nnnet",  # PR #25142 salvage (stop STT-failure chatter poisoning the LLM prompt; drop hardcoded English notice)
    "21066097+nnnet@users.noreply.github.com": "nnnet",  # PR #36024 salvage (dashboard: inline critical-CSS bootstrap for user themes)
    "vladimsmirnoff33@gmail.com": "londo161",  # PR #15795 salvage (redact status --all API keys; tolerate dict/str compression message shape)
    "neo.assistant2026@gmail.com": "neo-2026",  # PR #14026 salvage (clear input-blocking overlays on interrupt so the CLI doesn't freeze; #13618)
    "cypher@augmentl.com": "Nickperillo",  # PR #8008 salvage (Discord channel-name matching + flush pending sends on shutdown)
    "tenoryang@outlook.com": "MarioYounger",  # PR #9028 salvage (bash/sh heredoc approval, NFKC homograph fold, execute_code CREDS/BEARER/APIKEY env filter)
    "peet.wannasarnmetha@gmail.com": "peetwan",  # PR #51841 salvage (loopback ws-ping tuning + token-frame coalescing + loop heartbeat; #48445/#50005)
    "peter.skaronis@techimpossible.com": "Peterskaronis",  # PR #63889 salvage (mcp-oauth: WAF-safe redirect_host for loopback callback URIs)
    "297292863+Zyxxx-xxxyZ@users.noreply.github.com": "Zyxxx-xxxyZ",  # PR #54287 salvage (route frontend-polled inline RPCs to _LONG_HANDLERS; #48445/#50005)
    "kevenyanisme@gmail.com": "DataAdvisory",  # PR #9562 salvage (flatten multi-part user_message in codex intermediate-ack detector so vision turns don't crash)
    "huangsen365@gmail.com": "huangsen365",  # PR #42334 (CVE dependency pins + pin-drift guard)
    "telos@apex-z.com": "telos-oc",  # PR #14353 salvage (propagate custom_providers key_env into ProviderDef.api_key_env_vars; named + bare-custom self-heal paths)
    "256073454+Kolektori@users.noreply.github.com": "Kolektori",  # PR #6436 salvage (require approval for host-bound Docker commands; container guard fast-path)
    "41764686+LIC99@users.noreply.github.com": "LIC99",  # PR #4682 salvage (warn + default to manual on unknown approvals.mode; #4261)
    "carlosmcejas@gmail.com": "cmcejas",  # PR #41188 salvage (early Telegram auth gate before event build/observe; #40863)
    "ha-agent@homelab.4410.us": "oreoluwa",  # PR #49845 salvage (skip preflight content-type probe for OAuth MCP servers so OAuth discovery runs; Akiflow/Hospitable)
    "prathamesh290504@gmail.com": "PRATHAMESH75",  # PR #37550 salvage (ExecStopPost cgroup-orphan reaper to unblock systemd restart; #37454)
    "der@konsi.org": "konsisumer",  # PR #19608 salvage (read-modify-write merge in write_credential_pool to preserve concurrently-added credentials; #19566)
    "linyubin@users.noreply.github.com": "linyubin",  # PR #50228 salvage (eager fallback on persistent transport timeout/overloaded; #22277)
    "bradhallett@users.noreply.github.com": "bradhallett",  # PR #46948 salvage (force app exit after update/uninstall handoff on macOS; #46948)
    "65363919+coygeek@users.noreply.github.com": "coygeek",  # PR #37951 salvage (fail closed when provider env blocklist import fails; #37950)
    "5261694+djstunami@users.noreply.github.com": "djstunami",  # PR #5316 salvage / co-author (suppress transient check_fn flakes so subagents keep file/terminal tools; #21658 / #5304)
    "jmmaloney4@gmail.com": "jmmaloney4",  # PR #25206 salvage (re-select credential pool on primary runtime restore; #25205)
    "hmirin@users.noreply.github.com": "hmirin",
    "dale@dalenguyen.me": "dalenguyen",  # PR #53678 salvage (strip VIRTUAL_ENV/CONDA_PREFIX from terminal subprocess env; #23473)
    "liruixinch@outlook.com": "HexLab98",  # PR #53863 salvage (env-only proxy policy for auxiliary OpenAI clients on macOS; #53702)
    "blaryx@gmail.com": "Blaryxoff",  # PR #32602 salvage (deep-merge PUT /api/config to preserve unrelated sections; #13396)
    "diamondeyesfox@gmail.com": "DiamondEyesFox",  # PR #53351 salvage (rebaseline in-place compression flushes to prevent duplicate compacted rows; #9096)
    "piyrw9754@gmail.com": "rlaope",  # PR #35075 salvage (align cron invisible-unicode set with install-time scanner; #35075)
    "bukim0119@gmail.com": "bykim0119",  # PR #22335 salvage (honor "*" wildcard in DISCORD_ALLOWED_USERS; #22334)
    "rebel@rebels-Mac-Studio-2.local": "rebel0789",  # PR #47308 salvage (redact browser_type typed text across display surfaces; #47197)
    "267614622+agt-user@users.noreply.github.com": "agt-user",  # PR #48496 salvage (telegram CLOSE-WAIT polling heartbeat, #48495)
    "80915+DavidMetcalfe@users.noreply.github.com": "DavidMetcalfe",  # PR #52272 salvage (route reasoning-model thinking-timeouts to timeout not context_overflow + reasoning-specific guidance; #52271)
    "66773372+Tranquil-Flow@users.noreply.github.com": "Tranquil-Flow",  # PR #52623 salvage (auxiliary Anthropic base_url host validation; #52608)
    "nikshepsvn@gmail.com": "nikshepsvn",  # PR #27426 salvage (two-layer guard against hallucinated acp_command crashing the gateway on hosts with no ACP CLI)
    "65363919+coygeek@users.noreply.github.com": "coygeek",  # PR #37735 salvage (redact provider error text at api-server HTTP boundary; #37733)
    "moonsong@nousresearch.local": "Tranquil-Flow",  # PR #52623 salvage (auxiliary Anthropic base_url host validation; #52608)
    "baris@writeme.com": "isair",  # PR #50124 salvage (periodic FTS5 segment merge to curb write-lock contention; #54752)
    "140971685+Dr1985@users.noreply.github.com": "Dr1985",  # PR #42567 salvage (launchd supervision detection + status reporting; #42524)
    "8180647+herbalizer404@users.noreply.github.com": "herbalizer404",  # PR #49076 + #51835 salvage (auxiliary compression fallback: 403/session-usage payment errors + honor fallback chain when aux provider auth unavailable)
    "pyxl-dev@users.noreply.github.com": "pyxl-dev",  # PR #52230 salvage (include rate-limit in auxiliary capacity-error fallback gate; #52228)
    "yashiel@skyner.co.za": "yashiels",  # PR #53284 salvage (discord markdown table-to-bullet conversion; #21168)
    "46495124+yungchentang@users.noreply.github.com": "yungchentang",  # PR #53622 salvage (drain Telegram general send pool on pool timeout before retry; #53524)
    "15205536+595****0661@users.noreply.github.com": "595650661",  # PR #37851 salvage (classify MiniMax new_sensitive content filter → content_policy_blocked; #32421)
    "qWaitCrypto@users.noreply.github.com": "qWaitCrypto",  # PR #52534 salvage (preserve assistant tool_use cache_control marker in Anthropic conversion so cache breakpoints aren't dropped from the wire)
    "benbenwyb@gmail.com": "benbenlijie",  # PR #47205 salvage (named custom-provider extra_body + Z.AI Coding overload adaptive backoff; #50663)
    "dana@added-value.co.il": "Danamove",  # PR #46726 salvage (kill venv-resident pythonw gateway before recreating venv on Windows; #47036/#47557/#47910)
    "rcint@klaith.com": "rc-int",  # PR #9126 salvage / co-author (cap subagent summary size vs parent context overflow)
    "145739220+wgu9@users.noreply.github.com": "wgu9",  # PR #51468 salvage (WSL/no-systemd orphan gateway tracking, #51325)
    "165020384+uperLu@users.noreply.github.com": "uperLu",  # PR #50958 salvage (rename plugins/cron → plugins/cron_providers; #50872)
    "277269729+yusekiotacode@users.noreply.github.com": "yusekiotacode",  # PR #48706 salvage (anthropic OAuth login token endpoint → platform.claude.com; #45250/#49821)
    "minz0721@outlook.com": "s010mn",  # PR #29221 salvage (ollama-cloud reasoning_effort xhigh→max)
    "128256017+chriswesley4@users.noreply.github.com": "chriswesley4",  # PR #53185 salvage (re-enable titleBarOverlay on plain Linux; missing min/max/close regression)
    "rafael.millan@gmail.com": "RafaelMiMi",  # PR #42229 salvage (no-sandbox fallback for AppArmor-restricted Linux desktop launch)
    "jeevesassistant00@gmail.com": "jeeves-assistant",  # PR #50771 (computer-use CuaDriver vision capture routing)
    "21178861+ScotterMonk@users.noreply.github.com": "ScotterMonk",  # PR #50145 salvage (cron output truncation: adapter-aware chunking, #50126)
    "rrandqua@gmail.com": "TutkuEroglu",  # PR #50481 salvage (AGENTS.md stale token-lock adapter path)
    "f@trycua.com": "f-trycua",  # PR #50507 salvage (cross-platform computer_use; supersedes #44221/#30660)
    "fburka@noidea.de": "flewe",  # PR #47755 salvage (mcp-oauth: configurable redirect_uri for proxied callbacks, e.g. Tailscale Funnel)
    "pedro.m.simoes@gmail.com": "pmos69",  # PR #29474 salvage (native Antigravity OAuth provider; Gemini CLI sunset #29294/#49701)
    "mediratta01.pally@gmail.com": "orbisai0security",  # PR #9560 salvage (session.py path-traversal guard, V-009)
    "panghuer023@users.noreply.github.com": "panghuer023",  # PR #37994 salvage (interrupt unblocks pending gateway approval; #8697)
    "w.a.t.s.o.n.mk10@gmail.com": "natehale",  # PR #48678 salvage (typing indicator lingers after final reply)
    "0x0sec@gmail.com": "kn8-codes",  # PR #48422 salvage (rich messages opt-in default off)
    "liaoshiwu@gmail.com": "de1tydev",  # PR #10158 salvage (poll read-only for notify_on_complete watcher; #10156)
    "kurlyk@kurlyks-Mac-mini.local": "skabartem",  # PR #32867 salvage (atomic check-and-replace in _ensure_primary_openai_client; #32846)
    "szzhoujiarui@gmail.com": "szzhoujiarui-sketch",  # cron model.default salvage co-author (#45550)
    "rayjun0412@gmail.com": "rayjun",  # cron model.default salvage co-author (#43952)
    "96944678+sweetcornna@users.noreply.github.com": "sweetcornna",  # cron ticker-liveness salvage co-author (#33849)
    "izumi0uu@gmail.com": "izumi0uu",  # PR #49544 salvage (native rich reply echo; #49534)
    "zhangyingliang@outlook.com": "yingliang-zhang",  # PR #56084 (setup RPC pool routing; #57335)
    "dev@pixlmedia.no": "texhy",  # PR #27435 salvage (few-but-huge preflight compression gate; #27405)
    "qdaszx@naver.com": "qdaszx",  # PR #29190 salvage (non-blocking OSV malware preflight; #29184)
    "w31rdm4ch1n3z@protonmail.com": "w31rdm4ch1nZ",
    "xtpeeps@gmail.com": "x7peeps",
    "ahmad@madsgency.com": "ahmadashfq",
    "182213728+yinkev@users.noreply.github.com": "yinkev",  # scratch artifact preservation salvage
    "rratmansky@gmail.com": "rratmansky",
    "lkz-de@users.noreply.github.com": "lkz-de",
    "charles@salesondemand.io": "salesondemandio",
    "IamSanchoPanza@users.noreply.github.com": "IamSanchoPanza",
    "victor@rocketfueldev.com": "victor-kyriazakos",
    "87440198+JoaoMarcos44@users.noreply.github.com": "JoaoMarcos44",
    "joaomarcosdias444@gmail.com": "JoaoMarcos44",
    "neoguyver@icloud.com": "neoguyverx",  # PR #60526 salvage (fail-closed write syntax gate; #60525)
    "286497132+srojk34@users.noreply.github.com": "srojk34",
    "srojk34@users.noreply.github.com": "srojk34",  # legacy prefix-less noreply (PR #50098 salvage; #38763)
    "pinkiilqwq@users.noreply.github.com": "PINKIIILQWQ",  # PR #45035 salvage (resume-to-tip; #38763)
    "pink@PinkdeMacBook-Air.local": "PINKIIILQWQ",  # PR #45035 local git identity (resume-to-tip; #38763)
    "ailang323@163.com": "ailang323",  # PR #48682 salvage (compression-tip predicate; #38763)
    "59806492+sitkarev@users.noreply.github.com": "sitkarev",
    "zheng@omegasys.eu": "omegazheng",
    "220877172+james47kjv@users.noreply.github.com": "james47kjv",
    "yuhanglin@YuhangdeMac-mini.local": "1960697431",
    "admin@fent.quest": "XVVH",
    "despitemeguru@gmail.com": "definitelynotguru",
    "chaslui@outlook.com": "ChasLui",
    "rio.jeong@thebytesize.ai": "rio-jeong",
    "cdddo@users.noreply.github.com": "Cdddo",
    "carlos.dddo@gmail.com": "Cdddo",
    "yehaotian@xuanshudeMac-mini.local": "ArcanePivot",
    "dbeyer7@gmail.com": "benegessarit",
    "264773240+MrDiamondBallz@users.noreply.github.com": "MrDiamondBallz",
    "claudlos@agentmail.to": "claudlos",  # PR #52351 salvage (cron base_url exfil guard; #<salvagePR>)
    "94890352+Adolanium@users.noreply.github.com": "Adolanium",
    "kenmege@yahoo.com": "Kenmege",
    "tianying.x@eukarya.io": "xtymac",
    "dkobi16@gmail.com": "Diyoncrz18",
    "arnaud@nolimitdevelopment.com": "ali-nld",
    "sswdarius@gmail.com": "necoweb3",
    "wei-yujie@qq.com": "DNAlec",  # PR #61743 salvage (honor reset policy in #54878 stale-heal recovery)
    "joelbrilliant1@gmail.com": "joelbrilliant",  # PR #58486 salvage (session-expiry cleanup must not end row as agent_close)
    "bassisho@Mac-mini-bassis.local": "hydracoco7",  # PR #61382 salvage (id-less cron job freeze)
    "AlexFucuson9@users.noreply.github.com": "AlexFucuson9",  # PR #61209 salvage (hygiene compression data loss)
    "email@adambig.gs": "adambiggs",  # PR #43819 salvage (holographic shared SQLite connection)
    "koho.jung@outlook.com": "kohoj",  # PR #61667 salvage (nonce-CSP HTML session export)
    "t.chen@aftership.com": "cypctlinux",  # PR #52403 salvage (Slack bot/workflow auth before no-user-id guard)
    "30854794+YLChen-007@users.noreply.github.com": "YLChen-007",  # PR #26965 (approval remote command substitution)
    "1078345+egilewski@users.noreply.github.com": "egilewski",  # co-author, PR #40663
    "peterhao@Peters-MacBook-Air.local": "pinguarmy",
    "joe.rinaldijohnson@shopify.com": "joerj123",
    "adalsteinnhelgason@Aalsteinns-MacBook-Pro-3.local": "AIalliAI",
    "adalsteinnhelgason@users.noreply.github.com": "AIalliAI",
    "iamlukethedev@users.noreply.github.com": "iamlukethedev",
    "zhang.hz6666@gmail.com": "HaozheZhang6",
    "barronlroth@gmail.com": "barronlroth",
    "ondrej.drapalik@gmail.com": "OndrejDrapalik",
    "tomasz.panek@gmail.com": "tomekpanek",
    "philipadsouza@gmail.com": "PhilipAD",
    "zhuhaoyu0909@icloud.com": "underthestars-zhy",
    "raysun12142006@gmail.com": "yanxue06",
    "alberto.regalado@ymail.com": "ARegalado1",
    "alchemistchaos@protonmail.com": "AlchemistChaos",  # co-author only
    "gilad@smiti.ai": "giladbau",
    "yusufalweshdemir@gmail.com": "Dusk1e",
    "804436395@qq.com": "LaPhilosophie",
    "maxmitcham@mac.home": "maxtrigify",
    "ccook@nvms.com": "ccook1963",
    "libre-7@users.noreply.github.com": "libre-7",
    "kristian@agrointel.no": "kristianvast",
    "thomas.paquette@gmail.com": "RyTsYdUp",
    "techxacm@gmail.com": "ProgramCaiCai",
    "266365592+bmoore210@users.noreply.github.com": "bmoore210",
    "123150002+deaneeth@users.noreply.github.com": "deaneeth",
    "157839748+psionic73@users.noreply.github.com": "psionic73",
    "manishbyatroy@gmail.com": "manishbyatroy",
    "manusjs@users.noreply.github.com": "manus-use",  # PR #51129 salvage (Discord thread-starter dedup, #51057)
    "chilltulpa@gmail.com": "TheGardenGallery",
    "al@randomsnowflake.me": "randomsnowflake",
    "zakame@zakame.net": "zakame",
    "152110621+jiangkoumo@users.noreply.github.com": "jiangkoumo",
    "qinhaojie.exe@bytedance.com": "qin-ctx",
    "834740219@qq.com": "ViewWay",
    "matt@vestigial.dev": "m4dni5",
    "harjoth.khara@gmail.com": "harjothkhara",
    "129007007+HeLLGURD@users.noreply.github.com": "HeLLGURD",
    "290859878+synapsesx@users.noreply.github.com": "synapsesx",
    "157689911+itsflownium@users.noreply.github.com": "itsflownium",
    "dirtyren@users.noreply.github.com": "dirtyren",
    "juniperbevensee@users.noreply.github.com": "juniperbevensee",
    "krowd3v@users.noreply.github.com": "krowd3v",
    "dfein38347g@users.noreply.github.com": "dfein38347g",
    "nicktaylor@TheWorldofNick-Lappy.local": "thegoodguysla",
    "s96919@gmail.com": "s96919",
    "rasitakyol@hotmail.com": "rasitakyol",
    "thatgfsj@gmail.com": "Thatgfsj",
    "141703117+seagpt@users.noreply.github.com": "seagpt",
    "dr@nevernet.com": "davidrobertson",
    "59045242+HaiderSultanArc@users.noreply.github.com": "HaiderSultanArc",
    "jjadeo@gmail.com": "jjadeo-oss",
    "94815906+juanfradb@users.noreply.github.com": "juanfradb",
    "eva@100yen.org": "100yenadmin",
    "yakimenkoleksander228@gmail.com": "doxe0x",
    "a54983334@163.com": "Code-suphub",
    "78542984+Code-suphub@users.noreply.github.com": "Code-suphub",
    "shuangxinniao@gmail.com": "shuangxinniao",
    "hi@neueway.com": "brendandebeasi",
    "dorokuma@users.noreply.github.com": "dorokuma",
    "liuwei666888@users.noreply.github.com": "liuwei666888",
    "527711370@qq.com": "liuwei666888",
    "217401759+justinschille@users.noreply.github.com": "justinschille",
    "theoldwizard123@pm.me": "unsupportedpastels",
    "johnmlussier@gmail.com": "John-Lussier",
    "chenkun_lws@126.com": "bytesnail",  # PR #60360 salvage (--yolo startup ordering; #60328)
    "iamgexin@qq.com": "nullptr0807",  # PR #60956 salvage (gateway hygiene in-place compaction; #60947)
    "embwl0x@users.noreply.github.com": "embwl0x",  # PR #60810 salvage (channel directory offload)
    "caztronics@yahoo.com": "doncazper",
    "30668368+alex107ivanov@users.noreply.github.com": "alex107ivanov",
    "210088133+rungmc357@users.noreply.github.com": "rungmc357",
    "florian.rutishauser@outlook.com": "flo1t",
    "fanyang@microsoft.com": "fanyangCS",
    "bigstar0920@gmail.com": "bigstar0920",
    "hello@tanmaychoudhary.com": "tanmayxchoudhary",
    "285906080+AIalliAI@users.noreply.github.com": "AIalliAI",
    "waseemshahwan@users.noreply.github.com": "waseemshahwan",
    "hellno@users.noreply.github.com": "hellno",
    "perkintahmaz50@gmail.com": "devatnull",
    "marxb@protonmail.com": "Marxb85",
    "153708448+hunjaiboy@users.noreply.github.com": "yyzquwu",  # PR #47567 salvage (Matrix: register inbound handlers with wait_sync=True so _dispatch_sync's gather awaits them; without it mautrix fire-and-forgets and inbound intake has no completion point)
    "jearnest@velocityenergy.com": "jearnest11",  # PR #48700 salvage (multi-profile gateway flap: use node symlink's own parent, not .resolve() target, when building systemd/launchd service PATH so one profile's node path can't leak into every unit and force a perpetual daemon-reload restart loop)
    "tgmerritt@gmail.com": "tgmerritt",  # PR #43553 salvage (parse vLLM's token-based output-cap error format so over-cap max_tokens 400s reduce the output cap instead of death-looping into compression)
    "13277570+justin-cyhuang@users.noreply.github.com": "justin-cyhuang",
    "agent@tranquil-flow.dev": "Tranquil-Flow",
    "jason@hermes-jc": "jcjc81",
    "290862769+friendshipisover@users.noreply.github.com": "friendshipisover",
    "51421+MattKotsenas@users.noreply.github.com": "MattKotsenas",
    "92324143+ypwcharles@users.noreply.github.com": "ypwcharles",
    "mailtowbd@gmail.com": "marco0158",
    "157793278+jacobmansonlkevincc@users.noreply.github.com": "lkevincc0",
    "121278003+Cossackx@users.noreply.github.com": "Cossackx",  # PR #52528 salvage (Windows hermes-shim resolution + prefer --update on recovery; #52378)
    "97326386+Icather@users.noreply.github.com": "Icather",  # PR #45554 salvage (self-lock guard breaks Windows update-recovery infinite loop; #52378 / #45542)
    "--email": "andryypaez@gmail.com",
    "mucio@mucio.net": "francescomucio",
    "291572938+thestral123@users.noreply.github.com": "thestral123",
    "tkwong@inspiresynergy.com": "tkwong",
    "buihongduc132@gmail.com": "buihongduc132",
    "etheraura@protonmail.com": "EtherAura",  # PR #45205 salvage (Linux in-app update relaunch / GUI-skew terminal state)
    "valentt@users.noreply.github.com": "valentt",
    "devran.an12@gmail.com": "devorun",
    "xtpeeps@qq.com": "x7peeps",
    "sommerhoff@gmail.com": "andressommerhoff",
    "pwnda.zhang@dbappsecurity.com.cn": "x7peeps",
    "palkin.dominik@gmail.com": "skyc1e",
    "namredips@users.noreply.github.com": "namredips",
    "mihabubnjevic@gmail.com": "whoislikemiha",
    "m24927605@gmail.com": "m24927605",
    "gdeyoung@gmail.com": "gdeyoung",
    "gauravpatil2516@gmail.com": "GauravPatil2515",
    "fthakshn2727@gmail.com": "Sworntech-dev",
    "e10552@vip.officed.top": "jvradahellys24-art",
    "brett.bonner@infodesk.com": "bbopen",
    "berkayberksunn@gmail.com": "BBCrypto-web",
    "asimons81@gmail.com": "asimons81",
    "angelic805@gmail.com": "HwangJohn",
    "anderskev@gmail.com": "anderskev",
    "alloevil@hotmail.com": "alloevil",
    "aieng.abdullah.arif@gmail.com": "aieng-abdullah",
    "88768844+loes5050@users.noreply.github.com": "loes5050",
    "53877267+Tortugasaur@users.noreply.github.com": "Tortugasaur",
    "197037808+DrZM007@users.noreply.github.com": "DrZM007",
    "218993878+yapsrubricsz0@users.noreply.github.com": "yapsrubricsz0",
    "bhecfree@proton.me": "Railway9784",
    "graphanov@users.noreply.github.com": "graphanov",
    "antimatter543@users.noreply.github.com": "Antimatter543",
    "sluzalekmike@gmail.com": "mkslzk",
    "baolingao@users.noreply.github.com": "baolingao",
    "275304381+hakanpak@users.noreply.github.com": "hakanpak",
    "ludo.galabru@solana.org": "lgalabru",
    "johnjacobkenny@users.noreply.github.com": "johnjacobkenny",
    "chanyoung.kim@nota.ai": "channkim",
    "skyzh@mail.build": "xxchan",
    "stevenn.damatoo@gmail.com": "x1erra",
    "evansrory@gmail.com": "zimigit2020",
    "237263164+ft-ioxcs@users.noreply.github.com": "ft-ioxcs",
    "tharushkadinujaya05@gmail.com": "0xneobyte",
    "138671361+Veritas-7@users.noreply.github.com": "Veritas-7",
    "keiron@onehanded.com": "kmccammon",
    "268233388+CiarasClaws@users.noreply.github.com": "CiarasClaws",
    "amy@ravenwolf.de": "WolframRavenwolf",
    "github.com@wolfram.ravenwolf.de": "WolframRavenwolf",
    "895252509@qq.com": "895252509",
    "35259607+zxcasongs@users.noreply.github.com": "zxcasongs",
    "alfred@my-cloud.me": "alfred-smith-0",
    "tangtaizhong792@gmail.com": "tangtaizong666",
    "github@aldo.pw": "aldoeliacim",
    "max@c60spaceship.com": "MaxFreedomPollard",
    "achaljhawar03@gmail.com": "achaljhawar",
    "claytonchew@ClaytonMacMiniM4.local": "claytonchew",
    "hbentel@gmail.com": "hbentel",
    "JustinBao@outlook.com": "justinbao19",
    "kdunn926@gmail.com": "kdunn926",
    "mvanhorn@MacBook-Pro.local": "mvanhorn",
    "470766206@qq.com": "youjunxiaji",
    "mharris@parallel.ai": "NormallyGaussian",
    "roger@roger.local": "mollusk",
    "ted.malone@outlook.com": "temalo",
    "adityamalik2833@gmail.com": "alarcritty",
    "11778972+thegandhi@users.noreply.github.com": "thegandhi",
    "17757912+seansay@users.noreply.github.com": "seansay",
    "954341+jleclanche@users.noreply.github.com": "jleclanche",
    "5909384+hugues@users.noreply.github.com": "hugues",
    "43237+dplanella@users.noreply.github.com": "dplanella",
    "nousresearch@nousresearch.com": "nousresearch",
    "contact@nousresearch.com": "nousresearch",
    "erhart@compose.ai": "erhart",
    "m.guttmann@gmail.com": "mguttmann",
    "hello@jasonzhou.dev": "jasonzhou",
    "joao@emcasa.com": "joaocgreis",
    "jason@dataprep.app": "jason-dataprep",
    "saul@saul.pw": "saul",
    "zain@composio.ai": "zain",
    "root@hermes.local": "zain",
    "yasyf@google.com": "yasyf",
    "eugene@layer.ai": "eugeneyan",
    "eugene@eugeneyan.com": "eugeneyan",
    "e@eugeneyan.com": "eugeneyan",
    "yane@uber.com": "eugeneyan",
    "eugeneyan@fb.com": "eugeneyan",
    "kartik@wandb.com": "kartik",
    "yo@yoheinakajima.com": "yoheinakajima",
    "yohei@babyagi.org": "yoheinakajima",
    "h@yoheinakajima.com": "yoheinakajima",
    "yoheinakajima@gmail.com": "yoheinakajima",
    "yohei@run.associate.com": "yoheinakajima",
    "y@yoheinakajima.com": "yoheinakajima",
    "yohei.nakajima@gmail.com": "yoheinakajima",
    "armand@remix.com": "armand",
    "armand@motor.com": "armand",
    "armand.sala@gmail.com": "armand",
    "armand@adept.ai": "armand",
    "ar@armandsala.com": "armand",
    "ar@mdroid.com": "armand",
    "ar@adept.ai": "armand",
    "40355182+natolambert@users.noreply.github.com": "natolambert",
    "nate@fullstackdeeplearning.com": "natolambert",
    "josh@dataroots.io": "josh",
    "josh@joshbickett.com": "josh",
    "41853282+josh-bickett@users.noreply.github.com": "josh-bickett",
    "andres@ai.engineer": "andres",
    "andres@modal.com": "andres",
    "andres@e2b.dev": "andres",
    "andres@beroomers.com": "andres",
    "andres.iniesta.96@gmail.com": "andres",
    "andres.iniesta@adidas.com": "andres",
    "az@alea.com": "az",
    "a@z.com": "az",
    "az@z.com": "az",
    "az@alea.dev": "az",
    "az@alea.info": "az",
    "az@a.z": "az",
    "root@az.local": "az",
    "z@a.com": "az",
    "z@az.com": "az",
    "z@alea.com": "az",
    "a@alea.com": "az",
    "a@az.com": "az",
    "41898282+github-actions[bot]@users.noreply.github.com": "github-actions[bot]",
    "49699333+dependabot[bot]@users.noreply.github.com": "dependabot[bot]",
}

def load_author_map():
    """Load the author map from legacy dict + contributors directory."""
    author_map = LEGACY_AUTHOR_MAP.copy()
    contrib_dir = REPO_ROOT / "contributors" / "emails"
    if contrib_dir.is_dir():
        for f in contrib_dir.iterdir():
            if f.is_file():
                email = f.read_text(encoding="utf-8").strip()
                if email and "@" in email:
                    author_map[email] = f.stem
    return author_map


AUTHOR_MAP = load_author_map()
# ──────────────────────────────────────────────────────────────────────

# Release type: patch, minor, major
BUMP_TYPES = ["patch", "minor", "major"]

# Conventional Commit types
COMMIT_TYPES = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance Improvements",
    "refactor": "Code Refactoring",
    "docs": "Documentation",
    "style": "Styles",
    "test": "Tests",
    "build": "Build System",
    "ci": "Continuous Integration",
    "chore": "Chores",
    "revert": "Reverts",
}

def get_latest_tag():
    """Get the latest tag from the repo."""
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            encoding="utf-8",
            cwd=REPO_ROOT,
        ).strip()
        return tag
    except subprocess.CalledProcessError:
        return None

def get_commits_since(tag):
    """Get commits since a given tag."""
    cmd = ["git", "log", '--pretty=format:%H|%an|%ae|%s']
    if tag:
        cmd.append(f"{tag}..HEAD")
    return subprocess.check_output(cmd, encoding="utf-8", cwd=REPO_ROOT).strip().split("\n")

def parse_commit(commit):
    """Parse a commit message."""
    sha, author_name, author_email, subject = commit.split("|", 3)
    match = re.match(r"(\w+)(?:\((.+)\))?(!)?: (.+)", subject)
    if not match:
        return None, None, None, subject, sha, author_name, author_email
    type, scope, breaking, message = match.groups()
    return type, scope, breaking, message, sha, author_name, author_email

def generate_calver(date_str=None):
    """Generate a CalVer string (YYYY.M.D)."""
    if date_str:
        dt = datetime.strptime(date_str, "%Y.%m.%d")
    else:
        dt = datetime.utcnow()
    return f"{dt.year}.{dt.month}.{dt.day}"

def bump_semver(version, bump_type):
    """Bump a semantic version string."""
    major, minor, patch = map(int, version.split("."))
    if bump_type == "major":
        major += 1
        minor = 0
        patch = 0
    elif bump_type == "minor":
        minor += 1
        patch = 0
    elif bump_type == "patch":
        patch += 1
    return f"{major}.{minor}.{patch}"

def update_version_file(new_version):
    """Update the version in the __init__.py file."""
    content = VERSION_FILE.read_text(encoding="utf-8")
    content = re.sub(
        r"__version__ = \"(.+)\"",
        f'__version__ = "{new_version}"',
        content,
    )
    VERSION_FILE.write_text(content, encoding="utf-8")

def update_pyproject_file(new_version):
    """Update the version in pyproject.toml."""
    content = PYPROJECT_FILE.read_text(encoding="utf-8")
    content = re.sub(
        r'version = "(.+)"',
        f'version = "{new_version}"',
        content,
    )
    PYPROJECT_FILE.write_text(content, encoding="utf-8")

def update_acp_registry_manifest(new_version: str):
    """Update the version in acp_registry/agent.json."""
    if not ACP_REGISTRY_MANIFEST.exists():
        return
    try:
        manifest = json.loads(ACP_REGISTRY_MANIFEST.read_text(encoding="utf-8"))
        manifest["version"] = new_version
        ACP_REGISTRY_MANIFEST.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️  Could not update ACP registry manifest: {e}", file=sys.stderr)

def create_github_release(tag, changelog, dry_run):
    """Create a GitHub release."""
    if not shutil.which("gh"):
        print("gh command not found. Please install the GitHub CLI.", file=sys.stderr)
        sys.exit(1)
    
    cmd = ["gh", "release", "create", tag, "--notes", changelog, "--title", tag]
    if dry_run:
        cmd.append("--dry-run")
        print("Dry run: Would create GitHub release with the following command:")
        print(" ".join(cmd))
    else:
        print("Creating GitHub release...")
        subprocess.run(cmd, check=True, cwd=REPO_ROOT)

def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Hermes Agent Release Script")
    parser.add_argument("--bump", choices=BUMP_TYPES, help="Type of version bump")
    parser.add_argument("--publish", action="store_true", help="Create git tag and GitHub release")
    parser.add_argument("--first-release", action="store_true", help="This is the first release")
    parser.add_argument("--date", help="Override date for CalVer (YYYY.MM.DD)")
    args = parser.parse_args()

    if args.publish and not args.bump:
        print("Error: --bump is required with --publish", file=sys.stderr)
        sys.exit(1)

    latest_tag = None if args.first_release else get_latest_tag()
    if not latest_tag and not args.first_release:
        print("No previous tag found. Use --first-release for the initial release.", file=sys.stderr)
        sys.exit(1)

    commits = get_commits_since(latest_tag)
    if not commits:
        print("No new commits since the last release.")
        return

    # Group commits by type
    grouped_commits = defaultdict(list)
    breaking_changes = []
    for commit in commits:
        type, scope, breaking, message, sha, author_name, author_email = parse_commit(commit)
        if breaking:
            breaking_changes.append((message, sha, author_name, author_email))
        if type in COMMIT_TYPES:
            grouped_commits[type].append((scope, message, sha, author_name, author_email))

    # Generate changelog
    changelog = []
    if breaking_changes:
        changelog.append("### 🚨 BREAKING CHANGES")
        for msg, sha, name, email in breaking_changes:
            user = AUTHOR_MAP.get(email, name)
            changelog.append(f"* {msg} ({sha[:7]} by @{user})")
        changelog.append("")

    for type, title in COMMIT_TYPES.items():
        if type in grouped_commits:
            changelog.append(f"### {title}")
            for scope, msg, sha, name, email in sorted(grouped_commits[type]):
                user = AUTHOR_MAP.get(email, name)
                scope_str = f"**{scope}:** " if scope else ""
                changelog.append(f"* {scope_str}{msg} ({sha[:7]} by @{user})")
            changelog.append("")

    changelog_str = "\n".join(changelog)
    print("Generated Changelog:\n")
    print(changelog_str)

    # Determine new version
    calver = generate_calver(args.date)
    semver = "0.1.0"
    if latest_tag:
        try:
            _, last_semver = latest_tag.split("-v")
            if args.bump:
                semver = bump_semver(last_semver, args.bump)
            else:
                semver = last_semver
        except ValueError:
            print(f"Warning: Could not parse semantic version from tag '{latest_tag}'. Using 0.1.0.", file=sys.stderr)
    
    new_version = f"v{calver}-v{semver}"
    print(f"\nNew version: {new_version}")

    if args.publish:
        # Update version files
        update_version_file(new_version)
        update_pyproject_file(semver)  # pyproject.toml uses SemVer
        update_acp_registry_manifest(semver)

        # Commit version bump
        subprocess.run(["git", "add", str(VERSION_FILE), str(PYPROJECT_FILE), str(ACP_REGISTRY_MANIFEST)], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "commit", "-m", f"chore(release): {new_version}"], check=True, cwd=REPO_ROOT)

        # Create git tag
        subprocess.run(["git", "tag", new_version], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "push", "origin", new_version], check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "push"], check=True, cwd=REPO_ROOT)

        # Create GitHub release
        create_github_release(new_version, changelog_str, dry_run=False)
        print("Release created successfully!")
    else:
        print("\nDry run: No files were changed. To publish, use the --publish flag.")


if __name__ == "__main__":
    main()
