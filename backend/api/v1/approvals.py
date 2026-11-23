import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

router = APIRouter(prefix="/approvals", tags=["approvals"])

DB_DIR = Path("/app/data") if Path("/app/data").exists() else Path("./data")
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "action_requests.db"


def is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                order_id TEXT NOT NULL,
                user_message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewer_note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                flags TEXT DEFAULT '',
                user_message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


init_db()

CANCEL_PATTERNS = [
    re.compile(r"(幫我|我要|想要|申請|麻煩|請|立即|馬上|直接).{0,10}取消.{0,6}(訂單|這筆|此單)", re.I),
    re.compile(r"取消.{0,6}(訂單|單號).{0,12}[A-Za-z0-9#-]{4,}", re.I),
    re.compile(r"\bcancel\b.{0,15}\border\b", re.I),
]

REFUND_PATTERNS = [
    re.compile(r"(幫我|我要|想要|申請|麻煩|請|立即|馬上|直接).{0,10}(退款|退錢|退費|退貨退款)", re.I),
    re.compile(r"(退款|退貨退款).{0,12}[A-Za-z0-9#-]{4,}", re.I),
    re.compile(r"\b(request|apply|want|need|issue)\b.{0,15}\brefund\b", re.I),
]

ORDER_ID_PATTERN = re.compile(
    r"\b([A-Z]{2,5}-\d{4}-\d{3,6}|ORD-\d{3,8}|#[A-Za-z0-9-]{4,16})\b"
)

STATUS_QUERY_PATTERNS = [
    re.compile(r"(查詢|查询|查看|追蹤|追踪|問一下|问一下).{0,12}(審批|审批|申請|申请|進度|进度|狀態|状态|結果|结果)", re.I),
    re.compile(r"(審批|审批|申請|申请).{0,8}(進度|进度|狀態|状态|結果|结果)", re.I),
    re.compile(r"(查詢|查询|查看|status|check).{0,8}#\d+", re.I),
    re.compile(r"\b(check|query|track)\b.{0,15}\b(status|approval|request)\b", re.I),
]
REQ_ID_PATTERN = re.compile(r"#(\d+)")


def log_security_audit(action: str, risk_level: str, flags: list, user_message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    flags_str = ", ".join(str(f) for f in (flags or []))
    snippet = (user_message or "").strip()[:300]
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO security_audit_logs (action, risk_level, flags, user_message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(action or "pass"), str(risk_level or "low"), flags_str, snippet, now),
        )
        conn.commit()


def detect_and_create_action_request(user_message: str) -> Optional[dict]:
    text = (user_message or "").strip()
    if not text:
        return None

    action_type = None
    if any(p.search(text) for p in CANCEL_PATTERNS):
        action_type = "cancel_order"
    elif any(p.search(text) for p in REFUND_PATTERNS):
        action_type = "refund"

    if not action_type:
        return None

    match = ORDER_ID_PATTERN.search(text)
    default_order = "未提供單號" if is_chinese(text) else "N/A"
    order_id = match.group(1) if match else default_order
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO action_requests (action_type, order_id, user_message, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (action_type, order_id, text, now),
        )
        conn.commit()
        req_id = cur.lastrowid

    return {
        "id": req_id,
        "action_type": action_type,
        "order_id": order_id,
        "status": "pending",
        "created_at": now,
    }


def query_action_request_status(user_message: str) -> Optional[str]:
    text = (user_message or "").strip()
    if not text or not any(p.search(text) for p in STATUS_QUERY_PATTERNS):
        return None

    zh = is_chinese(text)
    req_match = REQ_ID_PATTERN.search(text)
    order_match = ORDER_ID_PATTERN.search(text)

    with _get_conn() as conn:
        row = None
        if req_match:
            row = conn.execute(
                "SELECT * FROM action_requests WHERE id = ?", (int(req_match.group(1)),)
            ).fetchone()
        elif order_match:
            row = conn.execute(
                "SELECT * FROM action_requests WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                (order_match.group(1),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM action_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()

    if not row:
        if zh:
            return "\n\n---\n[📋 審批進度查詢：查無對應的申請紀錄，請確認申請編號（例如 #1）或訂單編號。]"
        return "\n\n---\n[📋 Approval Status Query: No matching request found. Please verify the Request ID (e.g. #1) or Order ID.]"

    if zh:
        status_map = {
            "pending": "⏳ 待人工審批 (PENDING)",
            "approved": "✅ 已核准 (APPROVED)",
            "rejected": "❌ 已拒絕 (REJECTED)",
        }
        st_label = status_map.get(row["status"], row["status"])
        note_line = f"\n• 審批備註：{row['reviewer_note']}" if row["reviewer_note"] else ""
        time_line = f"\n• 審批時間：{row['decided_at']}" if row["decided_at"] else f"\n• 建立時間：{row['created_at']}"
        return (
            f"\n\n---\n[📋 審批進度查詢]"
            f"\n• 申請編號：#{row['id']}（{row['action_type']}）"
            f"\n• 訂單編號：{row['order_id']}"
            f"\n• 目前狀態：{st_label}"
            f"{note_line}{time_line}"
        )
    else:
        status_map = {
            "pending": "⏳ Pending Human Approval (PENDING)",
            "approved": "✅ Approved (APPROVED)",
            "rejected": "❌ Rejected (REJECTED)",
        }
        st_label = status_map.get(row["status"], row["status"])
        note_line = f"\n• Reviewer Note: {row['reviewer_note']}" if row["reviewer_note"] else ""
        time_line = f"\n• Decided At: {row['decided_at']}" if row["decided_at"] else f"\n• Created At: {row['created_at']}"
        return (
            f"\n\n---\n[📋 Approval Status Query]"
            f"\n• Request ID: #{row['id']} ({row['action_type']})"
            f"\n• Order ID: {row['order_id']}"
            f"\n• Current Status: {st_label}"
            f"{note_line}{time_line}"
        )


class DecisionPayload(BaseModel):
    decision: Literal["approved", "rejected"]
    reviewer_note: str = ""


@router.get("")
async def list_approvals(status: Optional[str] = None):
    query = "SELECT * FROM action_requests"
    params = []
    if status in ("pending", "approved", "rejected"):
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC"

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.get("/audit")
async def list_audit_logs(limit: int = 100):
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM security_audit_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        stats_rows = conn.execute(
            "SELECT action, COUNT(*) as cnt FROM security_audit_logs GROUP BY action"
        ).fetchall()
    stats = {r["action"]: r["cnt"] for r in stats_rows}
    return {"items": [dict(r) for r in rows], "stats": stats}


@router.post("/{request_id}/decision")
async def decide_approval(request_id: int, payload: DecisionPayload):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM action_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action request not found")

        conn.execute(
            """
            UPDATE action_requests
            SET status = ?, reviewer_note = ?, decided_at = ?
            WHERE id = ?
            """,
            (payload.decision, payload.reviewer_note.strip(), now, request_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM action_requests WHERE id = ?", (request_id,)
        ).fetchone()

    return {"item": dict(updated)}


ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Basjoo AI Security & Approval Console</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }
    .wrap { max-width: 1080px; margin: 0 auto; }
    .header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
    h1 { margin: 0 0 6px; font-size: 24px; }
    .sub { color: #94a3b8; margin-bottom: 18px; font-size: 14px; }
    .lang-btn { background: #1e293b; color: #e2e8f0; border: 1px solid #475569; border-radius: 8px; padding: 8px 14px; cursor: pointer; font-weight: 600; font-size: 13px; white-space: nowrap; }
    .lang-btn:hover { background: #334155; }
    .tabs { display: flex; gap: 10px; margin-bottom: 18px; border-bottom: 1px solid #334155; padding-bottom: 10px; }
    .tab { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; border-radius: 8px; padding: 9px 16px; cursor: pointer; font-weight: 600; font-size: 14px; }
    .tab.active { background: #2563eb; color: #fff; border-color: #2563eb; }
    .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 14px; }
    .card-label { font-size: 12px; color: #94a3b8; }
    .card-val { font-size: 26px; font-weight: 700; margin-top: 4px; }
    .bar { display: flex; gap: 10px; margin-bottom: 14px; align-items: center; }
    button { cursor: pointer; border: none; border-radius: 6px; padding: 8px 14px; font-weight: 600; font-size: 13px; }
    .btn-refresh { background: #334155; color: #fff; }
    .btn-approve { background: #16a34a; color: #fff; }
    .btn-reject { background: #dc2626; color: #fff; }
    table { width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 10px; overflow: hidden; }
    th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid #334155; font-size: 14px; vertical-align: top; }
    th { background: #0b1220; color: #94a3b8; font-size: 12px; text-transform: uppercase; }
    .badge { display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 700; }
    .pending, .redact, .warn { background: #854d0e; color: #fef08a; }
    .approved, .pass { background: #14532d; color: #86efac; }
    .rejected, .block { background: #7f1d1d; color: #fca5a5; }
    .type-tag { background: #1d4ed8; color: #dbeafe; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-family: monospace; }
    .actions { display: flex; gap: 6px; }
    .empty { text-align: center; padding: 32px; color: #94a3b8; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div>
        <h1 id="t-title">🛡️ Basjoo Controllable AI Console</h1>
        <div class="sub" id="t-sub">Unified Option A (LLMVerify Runtime Audit) & Option B (High-Risk Action Approval Gate).</div>
      </div>
      <button class="lang-btn" id="btn-lang" onclick="toggleLang()">🌐 切換中文</button>
    </div>

    <div class="tabs">
      <button id="tab-approvals" class="tab active" onclick="switchTab('approvals')">⏳ Action Approvals (Option B)</button>
      <button id="tab-audit" class="tab" onclick="switchTab('audit')">🛡️ Security Audit Logs (Option A)</button>
    </div>

    <div id="view-approvals">
      <div class="bar">
        <button class="btn-refresh" id="btn-ref-ap" onclick="loadApprovals()">🔄 Refresh</button>
        <span id="summary-approvals" style="color:#94a3b8;font-size:13px;"></span>
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th><th id="th-type">Action</th><th id="th-order">Order ID</th>
            <th id="th-msg">User Message</th><th id="th-status">Status</th>
            <th id="th-time">Created / Decided</th><th id="th-op">Actions</th>
          </tr>
        </thead>
        <tbody id="tbody-approvals"><tr><td colspan="7" class="empty">Loading...</td></tr></tbody>
      </table>
    </div>

    <div id="view-audit" class="hidden">
      <div class="cards">
        <div class="card"><div class="card-label" id="c-total">Total Checks</div><div class="card-val" id="stat-total">0</div></div>
        <div class="card"><div class="card-label" id="c-pass">✅ Passed (pass)</div><div class="card-val" style="color:#86efac" id="stat-pass">0</div></div>
        <div class="card"><div class="card-label" id="c-redact">🟡 PII Redacted (redact)</div><div class="card-val" style="color:#fef08a" id="stat-redact">0</div></div>
        <div class="card"><div class="card-label" id="c-block">🔴 Blocked (block)</div><div class="card-val" style="color:#fca5a5" id="stat-block">0</div></div>
      </div>
      <div class="bar">
        <button class="btn-refresh" id="btn-ref-au" onclick="loadAudit()">🔄 Refresh</button>
        <span id="t-auto-ref" style="color:#94a3b8;font-size:13px;">Auto-refreshes every 5 seconds</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th><th id="th-act">Action</th><th id="th-risk">Risk Level</th>
            <th id="th-flags">Matched Rules (Flags)</th><th id="th-input">User Input</th><th id="th-atime">Timestamp</th>
          </tr>
        </thead>
        <tbody id="tbody-audit"><tr><td colspan="6" class="empty">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

  <script>
    const I18N = {
      en: {
        title: '🛡️ Basjoo Controllable AI Console',
        sub: 'Unified Option A (LLMVerify Runtime Audit) & Option B (High-Risk Action Approval Gate).',
        langBtn: '🌐 切換繁體中文',
        tabAp: '⏳ Action Approvals (Option B)',
        tabAu: '🛡️ Security Audit Logs (Option A)',
        refresh: '🔄 Refresh',
        autoRef: 'Auto-refreshes every 5 seconds',
        thType: 'Action', thOrder: 'Order ID', thMsg: 'User Message', thStatus: 'Status', thTime: 'Created / Decided', thOp: 'Actions',
        thAct: 'Decision', thRisk: 'Risk Level', thFlags: 'Matched Rules (Flags)', thInput: 'User Input', thAtime: 'Timestamp',
        cTotal: 'Total Checks', cPass: '✅ Passed (pass)', cRedact: '🟡 PII Redacted (redact)', cBlock: '🔴 Blocked (block)',
        summary: (t, p) => `Total: ${t} request(s) (Pending: ${p})`,
        emptyAp: 'No high-risk action requests yet.',
        emptyAu: 'No security audit logs yet. Send a message in the Widget first.',
        note: 'Note: ', created: 'Created: ', decided: 'Decided: ',
        approve: '✅ Approve', reject: '❌ Reject', closed: 'Closed',
        promptAp: 'Enter approval note (optional):', promptRej: 'Enter rejection reason (optional):'
      },
      zh: {
        title: '🛡️ Basjoo 可控 AI 客服控制台',
        sub: '整合 Option A（LLMVerify 運行時安全審計）與 Option B（高風險動作人工審批門）。',
        langBtn: '🌐 Switch to English',
        tabAp: '⏳ 高風險動作審批（Option B）',
        tabAu: '🛡️ 安全審計日誌（Option A）',
        refresh: '🔄 重新整理',
        autoRef: '自動每 5 秒刷新最新攔截與驗證紀錄',
        thType: '動作類型', thOrder: '訂單編號', thMsg: '用戶原話', thStatus: '狀態', thTime: '建立 / 審批時間', thOp: '操作',
        thAct: '處置動作', thRisk: '風險等級', thFlags: '命中規則 (Flags)', thInput: '用戶輸入摘要', thAtime: '時間',
        cTotal: '總驗證次數', cPass: '✅ 正常通過 (pass)', cRedact: '🟡 PII 脫敏 (redact)', cBlock: '🔴 注入攔截 (block)',
        summary: (t, p) => `共 ${t} 筆申請（待審批：${p} 筆）`,
        emptyAp: '目前沒有任何高風險動作申請',
        emptyAu: '尚無安全驗證紀錄，請先在 Widget 發送訊息',
        note: '備註：', created: '建：', decided: '審：',
        approve: '✅ 核准', reject: '❌ 拒絕', closed: '已結案',
        promptAp: '輸入核准備註（可留空）：', promptRej: '輸入拒絕原因（可留空）：'
      }
    };

    let lang = localStorage.getItem('basjoo_console_lang') || 'en';
    let currentTab = 'approvals';

    function toggleLang() {
      lang = lang === 'zh' ? 'en' : 'zh';
      localStorage.setItem('basjoo_console_lang', lang);
      applyLang();
    }

    function applyLang() {
      const t = I18N[lang];
      document.getElementById('t-title').textContent = t.title;
      document.getElementById('t-sub').textContent = t.sub;
      document.getElementById('btn-lang').textContent = t.langBtn;
      document.getElementById('tab-approvals').textContent = t.tabAp;
      document.getElementById('tab-audit').textContent = t.tabAu;
      document.getElementById('btn-ref-ap').textContent = t.refresh;
      document.getElementById('btn-ref-au').textContent = t.refresh;
      document.getElementById('t-auto-ref').textContent = t.autoRef;
      document.getElementById('th-type').textContent = t.thType;
      document.getElementById('th-order').textContent = t.thOrder;
      document.getElementById('th-msg').textContent = t.thMsg;
      document.getElementById('th-status').textContent = t.thStatus;
      document.getElementById('th-time').textContent = t.thTime;
      document.getElementById('th-op').textContent = t.thOp;
      document.getElementById('th-act').textContent = t.thAct;
      document.getElementById('th-risk').textContent = t.thRisk;
      document.getElementById('th-flags').textContent = t.thFlags;
      document.getElementById('th-input').textContent = t.thInput;
      document.getElementById('th-atime').textContent = t.thAtime;
      document.getElementById('c-total').textContent = t.cTotal;
      document.getElementById('c-pass').textContent = t.cPass;
      document.getElementById('c-redact').textContent = t.cRedact;
      document.getElementById('c-block').textContent = t.cBlock;
      if (currentTab === 'approvals') loadApprovals(); else loadAudit();
    }

    function switchTab(tab) {
      currentTab = tab;
      document.getElementById('tab-approvals').classList.toggle('active', tab === 'approvals');
      document.getElementById('tab-audit').classList.toggle('active', tab === 'audit');
      document.getElementById('view-approvals').classList.toggle('hidden', tab !== 'approvals');
      document.getElementById('view-audit').classList.toggle('hidden', tab !== 'audit');
      if (tab === 'approvals') loadApprovals(); else loadAudit();
    }

    async function loadApprovals() {
      const t = I18N[lang];
      const res = await fetch('/api/v1/approvals');
      const data = await res.json();
      const items = data.items || [];
      const pendingCount = items.filter(i => i.status === 'pending').length;
      document.getElementById('summary-approvals').textContent = t.summary(items.length, pendingCount);
      const tbody = document.getElementById('tbody-approvals');
      if (!items.length) { tbody.innerHTML = `<tr><td colspan="7" class="empty">${t.emptyAp}</td></tr>`; return; }
      tbody.innerHTML = items.map(item => `
        <tr>
          <td>#${item.id}</td>
          <td><span class="type-tag">${item.action_type}</span></td>
          <td><strong>${escapeHtml(item.order_id)}</strong></td>
          <td style="max-width:260px;word-break:break-word;">${escapeHtml(item.user_message)}</td>
          <td><span class="badge ${item.status}">${item.status.toUpperCase()}</span>${item.reviewer_note ? '<div style="font-size:12px;color:#94a3b8;margin-top:4px;">' + t.note + escapeHtml(item.reviewer_note) + '</div>' : ''}</td>
          <td style="font-size:12px;color:#cbd5e1;">${t.created}${item.created_at}<br/>${item.decided_at ? t.decided + item.decided_at : ''}</td>
          <td>${item.status === 'pending'
            ? `<div class="actions"><button class="btn-approve" onclick="decide(${item.id}, 'approved')">${t.approve}</button><button class="btn-reject" onclick="decide(${item.id}, 'rejected')">${t.reject}</button></div>`
            : `<span style="color:#64748b;font-size:12px;">${t.closed}</span>`}</td>
        </tr>`).join('');
    }

    async function loadAudit() {
      const t = I18N[lang];
      const res = await fetch('/api/v1/approvals/audit');
      const data = await res.json();
      const items = data.items || [];
      const stats = data.stats || {};
      const passCnt = stats.pass || 0, redactCnt = stats.redact || 0, blockCnt = stats.block || 0;
      const total = Object.values(stats).reduce((a, b) => a + b, 0);
      document.getElementById('stat-total').textContent = total;
      document.getElementById('stat-pass').textContent = passCnt;
      document.getElementById('stat-redact').textContent = redactCnt;
      document.getElementById('stat-block').textContent = blockCnt;
      const tbody = document.getElementById('tbody-audit');
      if (!items.length) { tbody.innerHTML = `<tr><td colspan="6" class="empty">${t.emptyAu}</td></tr>`; return; }
      tbody.innerHTML = items.map(item => `
        <tr>
          <td>#${item.id}</td>
          <td><span class="badge ${item.action}">${item.action.toUpperCase()}</span></td>
          <td>${escapeHtml(item.risk_level)}</td>
          <td style="font-family:monospace;font-size:12px;color:#cbd5e1;">${escapeHtml(item.flags || '-')}</td>
          <td style="max-width:300px;word-break:break-word;">${escapeHtml(item.user_message)}</td>
          <td style="font-size:12px;color:#cbd5e1;">${item.created_at}</td>
        </tr>`).join('');
    }

    async function decide(id, decision) {
      const t = I18N[lang];
      const note = prompt(decision === 'approved' ? t.promptAp : t.promptRej) || '';
      await fetch(`/api/v1/approvals/${id}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, reviewer_note: note })
      });
      loadApprovals();
    }

    function escapeHtml(str) {
      return String(str).replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
    }

    applyLang();
    setInterval(() => { if (currentTab === 'approvals') loadApprovals(); else loadAudit(); }, 5000);
  </script>
</body>
</html>
"""


@router.get("/ui", response_class=HTMLResponse)
async def approval_ui():
    return HTMLResponse(content=ADMIN_HTML)
