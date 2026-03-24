import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"

export async function GET() {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Không có quyền truy cập" }, { status: 401 })
  }

  const apiKey = process.env.DEEKSEEK_KEY?.trim() || process.env.DEEPSEEK_KEY?.trim()
  const model = process.env.DEEPSEEK_MODEL?.trim() || "deepseek-chat"

  if (!apiKey) {
    return NextResponse.json({ error: "Chưa cấu hình API Key cho DeepSeek (DEEKSEEK_KEY / DEEPSEEK_KEY)" }, { status: 400 })
  }

  const startedAt = Date.now()
  try {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 15000)

    const res = await fetch("https://api.deepseek.com/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        temperature: 0.1,
        max_tokens: 10,
        messages: [{ role: "user", content: "Ping! Please reply with 'pong'." }],
      }),
      signal: controller.signal,
    })
    
    clearTimeout(timeout)

    if (!res.ok) {
      const text = await res.text().catch(() => "")
      throw new Error(`HTTP ${res.status}: ${text.slice(0, 100)}`)
    }

    const data = await res.json()
    const text = data.choices?.[0]?.message?.content?.trim() || ""
    
    return NextResponse.json({ 
      success: true, 
      message: `DeepSeek phản hồi thành công: "${text}"`,
      latencyMs: Date.now() - startedAt,
      model 
    })
  } catch (error: any) {
    return NextResponse.json({ 
      error: `Kết nối DeepSeek thất bại: ${error.message}` 
    }, { status: 500 })
  }
}
