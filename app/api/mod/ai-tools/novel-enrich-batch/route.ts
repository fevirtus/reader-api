import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

type EnrichedItem = {
  id: string
  title: string
  originalTitle: string
  authorName: string
  originalAuthorName: string
  description: string
  coverUrl?: string
  status: "Đang ra" | "Hoàn thành" | "Tạm ngưng"
  genresSuggested: string[]
  firstPublishYear?: number
  confidence: number
  source: string
  sourceUrl?: string
}

function normalizeText(value: string): string {
  return value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
}

function trimText(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value
  return `${value.slice(0, maxLength - 1).trimEnd()}...`
}

function buildBatchPrompt(novels: any[]) {
  const itemsText = novels.map(n => {
    const shortDesc = trimText(n.description || "", 1500)
    return `[ID: ${n.id}]\nTên: ${n.title}\nTên gốc: ${n.originalTitle || ""}\nTác giả: ${n.authorName}\nTác giả gốc: ${n.originalAuthorName || ""}\nThể loại: ${(n.genres || []).map((g: any) => g.genre.name).join(", ")}\nMô tả: ${shortDesc}`
  }).join("\n\n---\n\n")

  return [
    "Bạn là biên tập viên truyện dịch tiếng Trung.",
    "Nhiệm vụ: bổ sung thông tin bị thiếu (Tên gốc, Tác giả gốc, Mô tả, Thể loại) cho danh sách tác phẩm sau bằng cách tìm kiếm trên Qidian, JJWXC, v.v...",
    "Trường 'description': Nếu mô tả gốc đã chi tiết, chỉ cần sửa chính tả. Nếu trống/ngắn, hãy viết mới 1 đoạn giới thiệu dài chi tiết bám sát nội dung gốc (KHÔNG tóm tắt kiểu chung chung).",
    "Trạng thái (status) phải luôn là: Đang ra, Hoàn thành, hoặc Tạm ngưng.",
    "Kết quả BẮT BUỘC là 1 JSON Object chứa mảng 'results'. Mỗi item trong mảng phải có key 'id' khớp với báo cáo.",
    `Schema: {"results":[{"id":"","title":"","originalTitle":"","authorName":"","originalAuthorName":"","description":"","coverUrl":"","status":"Đang ra|Hoàn thành|Tạm ngưng","genresSuggested":[],"firstPublishYear":2020,"confidence":80,"source":"","sourceUrl":""}]}`,
    "Dưới đây là danh sách truyện cần xử lý:\n",
    itemsText
  ].join("\n")
}

function extractJsonCandidate(text: string): string {
  const trimmed = text.trim()
  if (!trimmed) return ""
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i)
  if (fenced?.[1]) return fenced[1].trim()
  const firstBrace = trimmed.indexOf("{")
  const lastBrace = trimmed.lastIndexOf("}")
  if (firstBrace >= 0 && lastBrace > firstBrace) {
    return trimmed.slice(firstBrace, lastBrace + 1)
  }
  return trimmed
}

export async function POST(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Không có quyền truy cập" }, { status: 401 })
  }

  const { novelIds } = await req.json()
  if (!Array.isArray(novelIds) || novelIds.length === 0 || novelIds.length > 20) {
    return NextResponse.json({ error: "novelIds không hợp lệ hoặc quá lớn (tối đa 20)" }, { status: 400 })
  }

  const accessWhere = session.user.role === "ADMIN"
    ? { id: { in: novelIds } }
    : {
        id: { in: novelIds },
        OR: [
          { uploaderId: session.user.id },
          { uploaderId: null },
        ],
      }

  const novels = await prisma.novel.findMany({
    where: accessWhere,
    select: {
      id: true,
      title: true,
      originalTitle: true,
      authorName: true,
      originalAuthorName: true,
      description: true,
      coverUrl: true,
      status: true,
      genres: { select: { genre: { select: { name: true } } } },
    }
  })

  if (novels.length === 0) {
    return NextResponse.json({ error: "Không tìm thấy truyện nào hợp lệ" }, { status: 404 })
  }

  const apiKey = process.env.DEEKSEEK_KEY?.trim() || process.env.DEEPSEEK_KEY?.trim()
  const model = process.env.DEEPSEEK_MODEL?.trim() || "deepseek-chat"

  if (!apiKey) {
    return NextResponse.json({ error: "Thiếu DEEPSEEK_KEY" }, { status: 400 })
  }

  const prompt = buildBatchPrompt(novels)
  
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 90000) // 90 seconds for batch
  const startedAt = Date.now()

  try {
    const res = await fetch("https://api.deepseek.com/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        temperature: 0.2,
        max_tokens: 2500,
        response_format: { type: "json_object" },
        messages: [
          { role: "system", content: "You are a helpful assistant. You must output only valid standard JSON object following the prompt schema, without markdown formatting." },
          { role: "user", content: prompt },
        ],
      }),
      signal: controller.signal,
    })
    
    clearTimeout(timeout)
    
    if (!res.ok) {
      const errorText = await res.text().catch(() => "")
      throw new Error(`HTTP ${res.status}: ${errorText.slice(0, 200)}`)
    }

    const data = await res.json()
    const text = data.choices?.[0]?.message?.content?.trim() || ""
    const jsonText = extractJsonCandidate(text)
    
    let parsed: any = null
    try {
      parsed = JSON.parse(jsonText)
    } catch {
      throw new Error("Phản hồi JSON bị hỏng")
    }

    const results = Array.isArray(parsed?.results) ? parsed.results : []
    
    return NextResponse.json({
      success: true,
      latencyMs: Date.now() - startedAt,
      model,
      count: results.length,
      results,
      sourceNovels: novels,
    })

  } catch (error: any) {
    clearTimeout(timeout)
    return NextResponse.json({ error: `DeepSeek Error: ${error.message}` }, { status: 500 })
  }
}
