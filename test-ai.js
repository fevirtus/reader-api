require('dotenv').config({ path: '.env' });

async function fetchJsonWithTimeout(url, init, timeoutMs = 25000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...init, signal: controller.signal, cache: "no-store" });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${text.slice(0, 500)}`);
    }
    return await res.json();
  } finally {
    clearTimeout(timeout);
  }
}

const prompt = "Please reply with { \"ready\": true } in JSON";

async function tryGoogle() {
  const apiKey = process.env.GOOGLE_AI_KEY;
  if (!apiKey) return console.log("Google: No key");
  try {
    const data = await fetchJsonWithTimeout(
      `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${apiKey}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ role: "user", parts: [{ text: prompt }] }],
          tools: [{ googleSearch: {} }],
          generationConfig: { temperature: 0.2, maxOutputTokens: 1400 },
        }),
      }
    );
    console.log("Google Success:", JSON.stringify(data).slice(0, 100));
  } catch(e) { console.log("Google Error:", e.stack || e.message); }
}

async function tryDeepSeek() {
  const apiKey = process.env.DEEKSEEK_KEY || process.env.DEEPSEEK_KEY;
  if (!apiKey) return console.log("DeepSeek: No key");
  try {
    const data = await fetchJsonWithTimeout(
        "https://api.deepseek.com/v1/chat/completions",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${apiKey}`,
          },
          body: JSON.stringify({
            model: "deepseek-chat",
            temperature: 0.2,
            max_tokens: 500,
            messages: [{ role: "user", content: prompt }],
          }),
        }
      );
      console.log("DeepSeek Success:", JSON.stringify(data).slice(0,100));
  } catch(e) { console.log("DeepSeek Error:", e.stack || e.message); }
}

async function run() {
    await tryGoogle();
    await tryDeepSeek();
}
run();
