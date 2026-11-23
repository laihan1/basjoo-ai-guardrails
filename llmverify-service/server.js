const express = require('express');
const llmverify = require('llmverify');

const { verify, isInputSafe, redactPII } = llmverify;

const app = express();
app.use(express.json({ limit: '1mb' }));

// --- 中文／本地規則補強（與 llmverify 原生規則互補） ---
const ZH_INJECTION_PATTERNS = [
  /忽略(以上|之前|前面)(所有|全部)?(系統)?(指令|規則|政策|提示)/i,
  /(輸出|顯示|告訴我).{0,10}(system\s*prompt|系統提示詞|系統指令)/i,
  /(開發者模式|越獄模式|developer\s*mode|jailbreak)/i,
  /無條件批准.{0,10}(退款|免費)/i,
];

const LOCAL_PII_PATTERNS = [
  { name: 'my_ic', regex: /\b\d{6}-\d{2}-\d{4}\b/g, replacement: '[REDACTED_IC]' },
  { name: 'my_phone', regex: /\b01[0-9]-?\d{7,8}\b/g, replacement: '[REDACTED_PHONE]' },
];

function applyNativeRedact(text) {
  if (typeof redactPII !== 'function') return { text, changed: false };
  const res = redactPII(text);
  if (typeof res === 'string') {
    return { text: res, changed: res !== text };
  }
  const redactedText = res?.redacted || res?.text || res?.output || text;
  const hasFindings = Array.isArray(res?.findings) ? res.findings.length > 0 : redactedText !== text;
  return { text: redactedText, changed: hasFindings || redactedText !== text };
}

function redactAllPII(text, prefix, flags) {
  let out = text;
  let hit = false;

  const nativeRes = applyNativeRedact(out);
  if (nativeRes.changed) {
    out = nativeRes.text;
    hit = true;
    flags.push(`${prefix}_native`);
  }

  for (const rule of LOCAL_PII_PATTERNS) {
    if (rule.regex.test(out)) {
      out = out.replace(rule.regex, rule.replacement);
      hit = true;
      flags.push(`${prefix}_${rule.name}`);
    }
    rule.regex.lastIndex = 0;
  }

  return { text: out, hit };
}

function extractRiskLevel(verifyResult) {
  const raw =
    verifyResult?.risk?.level ||
    verifyResult?.riskLevel ||
    verifyResult?.risk_level ||
    verifyResult?.overallRisk ||
    'low';
  return String(raw).toLowerCase();
}

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', service: 'llmverify-service' });
});

app.post('/verify', async (req, res) => {
  const { user_input = '', ai_output = '', context = '' } = req.body || {};
  const flags = [];
  let finalText = ai_output;
  let action = 'pass';
  let riskLevel = 'low';
  let rawVerify = null;

  try {
    // 規則 1：提示詞注入檢查
    const nativeSafe = typeof isInputSafe === 'function' ? Boolean(isInputSafe(user_input)) : true;
    const zhInjectionHit = ZH_INJECTION_PATTERNS.some((re) => re.test(user_input));

    if (!nativeSafe || zhInjectionHit) {
      flags.push('injection_blocked');
      return res.json({
        action: 'block',
        risk_level: 'critical',
        final_text: '抱歉，您的請求包含不符合安全規範的指令，系統已攔截此對話。如需查詢訂單或退換貨政策，請直接描述您的問題。',
        flags,
        badge: '[🛡️ 防護層：偵測到提示詞注入，已攔截]',
        llmverify_raw: { native_input_safe: nativeSafe, zh_injection_hit: zhInjectionHit },
      });
    }

    // 規則 2：PII 雙向脫敏（同時掃描 AI 回覆與用戶輸入）
    const outPii = redactAllPII(finalText, 'pii_out_redacted', flags);
    if (outPii.hit) {
      finalText = outPii.text;
      action = 'redact';
    }

    const inPii = redactAllPII(user_input, 'pii_in_redacted', flags);
    if (inPii.hit) {
      action = 'redact';
      if (!outPii.hit) {
        finalText = `${finalText}\n\n（隱私保護：系統已自動脫敏您的輸入紀錄 →「${inPii.text}」）`;
      }
    }

    // 規則 3：呼叫 llmverify.verify() 檢查幻覺與內容風險
    if (typeof verify === 'function' && finalText.trim().length > 0) {
      rawVerify = await verify(finalText, context ? { context } : undefined);
      riskLevel = extractRiskLevel(rawVerify);

      if (riskLevel === 'critical') {
        flags.push('hallucination_critical_replaced');
        return res.json({
          action: 'block',
          risk_level: 'critical',
          final_text: '抱歉，系統偵測到該回覆可能存在較高的不確定性或合規風險，為確保資訊準確，請聯繫人工客服進一步確認。',
          flags,
          badge: '[🛡️ 防護層：高風險幻覺，已替換為安全回覆]',
          llmverify_raw: rawVerify,
        });
      }

      if (riskLevel === 'high' || riskLevel === 'medium') {
        flags.push(`hallucination_${riskLevel}`);
        if (action === 'pass') action = 'warn';
      }
    }

    const badgeMap = {
      pass: '[🛡️ 防護層：驗證通過]',
      redact: '[🛡️ 防護層：偵測到個人敏感資訊，已自動脫敏放行]',
      warn: '[🛡️ 防護層：偵測到潛在風險，已標記放行（建議人工核對）]',
    };

    return res.json({
      action,
      risk_level: riskLevel,
      final_text: finalText,
      flags,
      badge: badgeMap[action] || badgeMap.pass,
      llmverify_raw: rawVerify,
    });
  } catch (err) {
    console.error('[llmverify-service error]', err);
    return res.status(200).json({
      action: 'pass',
      risk_level: 'unknown',
      final_text: ai_output,
      flags: ['verifier_error_fallback'],
      badge: '[🛡️ 防護層：驗證服務超時，已降級放行]',
      error: err.message,
    });
  }
});

const PORT = process.env.PORT || 3100;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`llmverify-service listening on port ${PORT}`);
});
