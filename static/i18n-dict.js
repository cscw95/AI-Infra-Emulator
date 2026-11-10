/* AI Infra Emulator — KO→EN translation dictionary (VR NVL72 Digital Twin).
 *
 * Loaded before static/i18n.js. Two sections:
 *   dict     — exact full-string matches (trimmed). Highest priority.
 *   patterns — [regex, replacement] for strings containing numbers/ids the
 *              exact map can't enumerate. `$1`.. refer to capture groups.
 *
 * Covers BOTH dashboard-native UI strings (static index.html) and the only
 * backend-surfaced Korean that reaches the DOM (site names STT 가산 / IGIS
 * 안산 — all fault/event/alarm Korean stays in un-rendered alert lists).
 * Terminology aligned with neocloud-consoles (Tenant / Rack / Isolation …).
 */
window.NC_I18N = window.NC_I18N || {};
window.NC_I18N.dict = Object.assign(window.NC_I18N.dict || {}, {

  // ── header / sub ──────────────────────────────────────────────────
  "물리 트윈 · HW: Dell · PowerEdge XE9812 Compute/DCGM · VAST Storage · UFM/IB · NetQ/Converged · PowerCool DLC · DPU/BlueField · iDRAC10 Redfish · NICo Emulator(:9000)가 제어":
    "Physical twin · HW: Dell · PowerEdge XE9812 Compute/DCGM · VAST Storage · UFM/IB · NetQ/Converged · PowerCool DLC · DPU/BlueField · iDRAC10 Redfish · controlled by NICo Emulator(:9000)",
  "물리 규격 기반 트윈: Vera Rubin NVL72(72 Rubin GPU/36 Vera CPU), Dell PowerEdge XE9812 · iDRAC10, Dell PowerCool CDU C7000, VAST Data Platform (DASE), Quantum-X800 IB, Dell PowerSwitch SN5610 (Spectrum-X), ConnectX-9/BlueField-4. HW vendor: Dell, storage: VAST Data.":
    "Physical-spec-based twin: Vera Rubin NVL72(72 Rubin GPU/36 Vera CPU), Dell PowerEdge XE9812 · iDRAC10, Dell PowerCool CDU C7000, VAST Data Platform (DASE), Quantum-X800 IB, Dell PowerSwitch SN5610 (Spectrum-X), ConnectX-9/BlueField-4. HW vendor: Dell, storage: VAST Data.",
  "한국어": "Korean",

  // ── site names (backend-surfaced) ─────────────────────────────────
  "STT 가산": "STT Gasan",
  "IGIS 안산": "IGIS Ansan",
  "STT 가산 · Spectrum-X": "STT Gasan · Spectrum-X",
  "IGIS 안산 · Spectrum-X": "IGIS Ansan · Spectrum-X",
  "STT 가산 · gasan": "STT Gasan · gasan",
  "IGIS 안산 · ansan": "IGIS Ansan · ansan",
  "사이트 gasan": "Site gasan",
  "사이트 ansan": "Site ansan",
  "가산": "Gasan",
  "안산": "Ansan",

  // ── Twin Control / cluster bulk control ───────────────────────────
  "— 전체 클러스터 140랙 설정·제어 (power / cap / workload / cordon) · 실텔레메트리 반영":
    "— whole-cluster 140-rack config·control (power / cap / workload / cordon) · live telemetry reflected",
  "전체 랙 제어 — 데모": "All-rack control — demo",
  "— 범위 일괄 제어 (power / cap / workload / cordon) · 1차 클릭 후 3초 내 재클릭으로 실행 (2단계 확인)":
    "— scope bulk control (power / cap / workload / cordon) · click again within 3s to execute (2-step confirm)",
  "범위:": "Scope:",
  "전체 SU": "All SU",
  "전체 사이트": "All sites",
  "전원 On": "Power On",
  "전원 Off": "Power Off",
  "재시작": "Restart",
  "Power Cap 적용": "Apply Power Cap",
  "Cap 해제": "Uncap",
  "프로필 적용": "Apply profile",
  "값": "Value",
  "결과": "Result",
  "Power Cap 값을 먼저 입력하세요": "Enter a Power Cap value first",
  "확인 — 실행": "Confirm — execute",
  "주의: 테넌트 워크로드 영향 — 해당 범위의 실행 중 워크로드가 중단될 수 있습니다. 3초 내 재클릭 시 즉시 실행됩니다.":
    "Caution: tenant workload impact — running workloads in this scope may be interrupted. Click again within 3s to execute immediately.",

  // ── applied rack settings / rack-scope table ──────────────────────
  "적용된 랙 설정": "Applied rack config",
  "— 기본값(on · uncap · steady · uncordon)과 다른 랙 · 행별 즉시 해제":
    "— racks differing from defaults (on · uncap · steady · uncordon) · per-row instant reset",
  "기본값과 다른 랙 없음 — 전체 on · uncap · steady · uncordon":
    "No racks differ from defaults — all on · uncap · steady · uncordon",
  "복원": "Restore",
  "기본값 복원": "Restore defaults",
  "랙 제어 — 데모 ·": "Rack control — demo ·",
  "cap 없음": "no cap",
  "cordon 없음": "no cordon",
  "obs 상태 미확인 (랙 미선택)": "obs status unknown (no rack selected)",
  "랙 미선택": "no rack selected",

  // ── single-rack power-control card buttons ────────────────────────
  "제어 (데모)": "Control (demo)",

  // ── Fabric (IB topology) ──────────────────────────────────────────
  "— 사이트별 독립 패브릭 · \"토폴로지 구성 뷰\" 클릭 → 듀얼 Fabric-A/B IB 토폴로지 팝업":
    "— independent fabric per site · click \"topology view\" → dual Fabric-A/B IB topology popup",
  "토폴로지 구성 뷰": "Topology view",
  "IB 패브릭 토폴로지 구성 뷰 — 듀얼 네트워크 Fabric-A/B":
    "IB fabric topology view — dual-network Fabric-A/B",
  "Fabric-A + B (양쪽)": "Fabric-A + B (both)",
  "Fabric-A spine · 링크": "Fabric-A spine · links",
  "Fabric-B spine · 링크": "Fabric-B spine · links",
  "SU (랙 집약)": "SU (rack aggregate)",
  "--- degraded 링크 (파선)": "--- degraded links (dashed)",
  "SU 단위 집약 뷰 (랙 개수 라벨)": "SU aggregate view (rack-count label)",
  "— Quantum-X800 개별 스위치 · UFM Enterprise 에뮬레이션":
    "— Quantum-X800 per-switch · UFM Enterprise emulation",
  "— 클러스터 공통": "— cluster-wide",
  "— 클러스터·뷰 롤업 · 체크포인트 write burst 반영":
    "— cluster·view rollup · checkpoint write burst reflected",
  "테넌트 파티션 없음 — 전체 미할당 풀": "No tenant partitions — entirely unallocated pool",
  "미할당 풀": "Unallocated pool",

  // ── Storage (VAST) ────────────────────────────────────────────────
  "— 사이트별 AI Storage 1식 · DASE(CBox/DBox) · VMS 에뮬레이션 · 카드별 장애 주입 — 데모":
    "— one AI Storage set per site · DASE(CBox/DBox) · VMS emulation · per-card fault injection — demo",
  "— NFS/S3 export · 테넌트 attachment 파생 · quota/QoS":
    "— NFS/S3 export · tenant attachment derived · quota/QoS",

  // ── Converged / NetQ ──────────────────────────────────────────────
  "— 스토리지·관리 N-S converged rail · CX-9 dual-port + BF-4 · Spectrum-X storage fabric · 사이트별 장애 주입 — 데모":
    "— storage·management N-S converged rail · CX-9 dual-port + BF-4 · Spectrum-X storage fabric · per-site fault injection — demo",

  // ── device fault-injection controls (common) ──────────────────────
  "장애 주입 — 데모": "Fault injection — demo",
  "· 행별 장애 주입 — 데모": "· per-row fault injection — demo",
  "· 행별 장애 주입 — 데모 (1차 클릭 후 3초 내 재클릭)":
    "· per-row fault injection — demo (click again within 3s)",
  "주의(데모): 장애 주입 — 해당 장비 텔레메트리·알림에 즉시 반영됩니다. 3초 내 재클릭 시 실행.":
    "Caution (demo): fault injection — immediately reflected in the device's telemetry·alerts. Click again within 3s to execute.",
  "복구 (데모): 대상 없음": "Recovery (demo): none",
  "대상 없음": "none",

  // ── Provisioning tab ──────────────────────────────────────────────
  "— PXE → DHCP → iPXE → OS → Host Agent · 행 클릭 → DPU 기반 IP 할당·OS 설치 상세 팝업 · buttons drive the boot state machine (writes)":
    "— PXE → DHCP → iPXE → OS → Host Agent · click a row → DPU-based IP allocation·OS install detail popup · buttons drive the boot state machine (writes)",
  "사이트:": "Site:",
  "tray 검색 (예: tray-07)": "search tray (e.g. tray-07)",
  "클릭 → DPU 기반 IP 할당·OS 설치 상세": "click → DPU-based IP allocation·OS install detail",

  // ── Tray-ops panel (KPI + inflight + history) ─────────────────────
  "— tick(2.5s)당 1단계 · tenant rejoin · KPI(MTTR) · 트레이 팝업에서 실행 — 데모":
    "— one step per tick(2.5s) · tenant rejoin · KPI(MTTR) · run from tray popup — demo",
  "트레이 작업 — 재기동 · HW 교체": "Tray operations — Reboot · HW replacement",
  "MTTR 평균": "MTTR avg",
  "IP 할당": "IP allocation",
  "OS 설치": "OS install",
  "rejoin 성공": "rejoin success",
  "진행 중인 트레이 작업 없음 — 위 트레이 행 클릭 → 팝업에서 재기동/HW 교체 실행 (데모)":
    "No tray operations in progress — click a tray row above → run reboot/HW replacement from the popup (demo)",
  "트레이 작업 이력 없음": "No tray operation history",

  // ── Tray detail popup ─────────────────────────────────────────────
  "현재 상태 요약": "Current status summary",
  "재프로비저닝": "Reprovision",
  "재기동": "Reboot",
  "HW 교체": "HW replacement",
  "(데모 · 2단계 확인)": "(Demo · 2-step confirm)",
  "담당 DPU": "Assigned DPU",
  "DPU 기반 IP 할당": "DPU-based IP allocation",
  "DHCP 서버": "DHCP server",
  "DHCP 서버 (DPU)": "DHCP server (DPU)",
  "Lease 시간": "Lease time",
  "Lease 상태": "Lease state",
  "할당 시각": "Allocated at",
  "DNS 등록": "DNS registration",
  "활성 DHCP lease 없음 — 재프로비저닝 실행 시 DPU-DHCP가 lease를 발급합니다.":
    "No active DHCP lease — DPU-DHCP will issue a lease when reprovisioning runs.",
  "전원 On (Redfish)": "Power On (Redfish)",
  "전원 Off (Redfish ForceOff)": "Power Off (Redfish ForceOff)",
  "OS 설치 / 부트 단계": "OS install / boot stage",
  "트레이 작업 진행 — 재기동": "Tray operation in progress — Reboot",
  "트레이 작업 진행 — HW 교체": "Tray operation in progress — HW replacement",
  "주의(데모): 트레이 재기동 — 테넌트 GPU가 일시 unavail 됩니다. 3초 내 재클릭 시 실행.":
    "Caution (demo): tray reboot — tenant GPUs become temporarily unavailable. Click again within 3s to execute.",
  "주의(데모): 트레이 HW 교체 — 테넌트 GPU가 일시 unavail 됩니다. 3초 내 재클릭 시 실행.":
    "Caution (demo): tray HW replacement — tenant GPUs become temporarily unavailable. Click again within 3s to execute.",
  "재기동 시작 — 단계 진행 중…": "Reboot started — steps in progress…",
  "HW 교체 시작 — 단계 진행 중…": "HW replacement started — steps in progress…",

  // ── boot / provisioning UI steps (label + description) ────────────
  "PXE 요청": "PXE request",
  "UEFI 네트워크 부트 — DPU 경유 PXE Discover": "UEFI network boot — PXE Discover via DPU",
  "DHCP Lease — DPU 기반 IP 할당": "DHCP Lease — DPU-based IP allocation",
  "DPU-DHCP 서버가 host lease 발급 · DNS A 레코드 등록":
    "DPU-DHCP server issues host lease · registers DNS A record",
  "boot.ipxe 스크립트 chainload": "boot.ipxe script chainload",
  "OS 이미지 다운로드 · kickstart/cloud-init": "OS image download · kickstart/cloud-init",
  "vmlinuz / initrd 스트리밍 · 자동 설치 적용": "vmlinuz / initrd streaming · automated install applied",
  "재부팅": "Reboot",
  "설치 완료 후 로컬 디스크 부팅": "Boot from local disk after install completes",
  "lifecycle Ready · NICo 등록 완료": "lifecycle Ready · NICo registration complete",

  // ── stepper / op-step status tags ─────────────────────────────────
  "완료": "Done",
  "진행 중": "In progress",
  "대기": "Pending",
  "건너뜀 — tenant 없음": "Skipped — no tenant",

  // ── modal close ───────────────────────────────────────────────────
  "닫기 ✕": "Close ✕",
});

window.NC_I18N.patterns = (window.NC_I18N.patterns || []).concat([
  // footer status line (time + section vary)
  [/^(last updated .+ · AI Infra Emulator) — 물리 트윈\/장비$/, "$1 — Physical Twin / Devices"],
  // power-cap hint with live IT power
  [/^IT ([\d.]+) kW · 1차 클릭 후 3초 내 재클릭으로 실행$/,
    "IT $1 kW · click again within 3s to execute"],
  // scope select "all racks" option
  [/^전체 (\d[\d,]*)랙$/, "All $1 racks"],
  // tenant partition label — "<tenant> <pkey> · N랙"
  [/^(.+) · (\d+)랙$/, "$1 · $2 racks"],
  // UFM link summary — "— 링크 5344/5344 active · degraded"
  [/^— 링크 (.+)$/, "— Links $1"],
  // SU aggregate composite header (site name + English/number middle preserved)
  [/^STT 가산([\s\S]*)SU 단위 집약 뷰 \(랙 개수 라벨\)$/,
    "STT Gasan$1SU aggregate view (rack-count label)"],
  [/^IGIS 안산([\s\S]*)SU 단위 집약 뷰 \(랙 개수 라벨\)$/,
    "IGIS Ansan$1SU aggregate view (rack-count label)"],
  // "su-N · <site> · state …" and "su-N-rack-MM · <site>"
  [/^(su-\S+ · )STT 가산(.*)$/, "$1STT Gasan$2"],
  [/^(su-\S+ · )IGIS 안산(.*)$/, "$1IGIS Ansan$2"],
  // tray-op step done tag with duration
  [/^완료 · ([\d.]+)s$/, "Done · $1s"],
  // tray-op inflight hint (KPI panel)
  [/^tenant (.+) · 경과 (\d+)s$/, "tenant $1 · elapsed $2s"],
  // tray-op detail hint (modal right pane)
  [/^· tenant (.+) · 경과 (\d+)s · tick당 1단계$/,
    "· tenant $1 · elapsed $2s · one step per tick"],
  // device fault inject / recover result messages
  [/^주입 \(데모\): (.+)$/, "Injected (demo): $1"],
  [/^복구 \(데모\): (.+)$/, "Recovery (demo): $1"],
  // obs status unknown (rack id varies)
  [/^obs 상태 미확인 \((.+)\)$/, "obs status unknown ($1)"],
  // generic failure prefix
  [/^실패: (.*)$/, "Failed: $1"],
]);
