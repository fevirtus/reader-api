import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

type EnrichedItem = {
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

type AttemptStatus = {
  provider: "google" | "openrouter" | "deepseek"
  model: string
  status: "success" | "failed" | "skipped"
  message?: string
  latencyMs?: number
}

type ProviderResult = {
  provider: "google" | "openrouter" | "deepseek"
  model: string
  results: EnrichedItem[]
}

type GeminiResponse = {
  candidates?: Array<{
    content?: {
      parts?: Array<{ text?: string }>
    }
  }>
}

type ChatResponse = {
  choices?: Array<{
    message?: {
      content?: string
    }
  }>
}

type OpenRouterModelsResponse = {
  data?: Array<{
    id?: string
  }>
}

const OPENROUTER_PAUSED = (process.env.OPENROUTER_PAUSED ?? "true").toLowerCase() !== "false"

function hasMeaningfulDescription(value: string): boolean {
  const normalized = normalizeText(value)
  if (!normalized || normalized.length < 40) return false

  const placeholders = [
    "chua co gioi thieu",
    "khong co gioi thieu",
    "dang cap nhat",
    "dang update",
    "updating",
    "no description",
    "khong ro",
    "chua ro",
  ]

  return !placeholders.some((item) => normalized.includes(item))
}

function buildGeneratedDescription(
  title: string,
  authorName: string,
  genres: string[],
  status: "Đang ra" | "Hoàn thành" | "Tạm ngưng"
): string {
  const genreText = genres.length > 0 ? genres.slice(0, 3).join(", ") : "tiểu thuyết mạng"
  const statusText = status === "Đang ra" ? "đang ra" : status === "Hoàn thành" ? "đã hoàn thành" : "tạm ngưng"

  return trimText(
    `${title} là một tác phẩm ${genreText} của ${authorName}. Câu chuyện được hệ thống tóm lược lại từ những thông tin đã tìm thấy trên web và hiện ở trạng thái ${statusText}. Đây là đoạn mô tả thay thế được tạo tự động khi mô hình chưa trả về phần giới thiệu đủ chi tiết, giúp biên tập viên có sẵn nội dung để rà soát và chỉnh sửa tiếp.`,
    1200
  )
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

function inferStatus(subjects: string[], description: string): "Đang ra" | "Hoàn thành" | "Tạm ngưng" {
  const haystack = normalizeText(`${subjects.join(" ")} ${description}`)

  if (haystack.includes("completed") || haystack.includes("finished") || haystack.includes("full text")) {
    return "Hoàn thành"
  }

  if (haystack.includes("hiatus") || haystack.includes("on hold") || haystack.includes("paused")) {
    return "Tạm ngưng"
  }

  return "Đang ra"
}

function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
  const num = typeof value === "number" ? value : Number(value)
  if (!Number.isFinite(num)) return fallback
  return Math.max(min, Math.min(max, num))
}

function toOptionalUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  const trimmed = value.trim()
  if (!trimmed) return undefined
  if (!/^https?:\/\//i.test(trimmed)) return undefined
  return trimmed
}

function coerceStatus(value: unknown, genres: string[], description: string): "Đang ra" | "Hoàn thành" | "Tạm ngưng" {
  if (typeof value === "string") {
    const normalized = normalizeText(value)
    if (normalized.includes("hoan thanh") || normalized.includes("completed") || normalized.includes("finished")) {
      return "Hoàn thành"
    }
    if (normalized.includes("tam") || normalized.includes("ngung") || normalized.includes("hiatus") || normalized.includes("paused")) {
      return "Tạm ngưng"
    }
    if (normalized.includes("dang") || normalized.includes("ongoing")) {
      return "Đang ra"
    }
  }

  return inferStatus(genres, description)
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

function toItem(raw: any, novelTitle: string): EnrichedItem | null {
  const title = typeof raw?.title === "string" ? raw.title.trim() : ""
  if (!title) return null

  const originalTitle = typeof raw?.originalTitle === "string" && raw.originalTitle.trim()
    ? raw.originalTitle.trim()
    : title

  const authorName = typeof raw?.authorName === "string" && raw.authorName.trim()
    ? raw.authorName.trim()
    : "Chưa rõ"

  const originalAuthorName = typeof raw?.originalAuthorName === "string" && raw.originalAuthorName.trim()
    ? raw.originalAuthorName.trim()
    : authorName

  const genres = Array.isArray(raw?.genresSuggested)
    ? raw.genresSuggested
        .map((g: unknown) => (typeof g === "string" ? g.trim() : ""))
        .filter(Boolean)
        .slice(0, 10)
    : []

  const status = coerceStatus(raw?.status, genres, typeof raw?.description === "string" ? raw.description.trim() : "")

  const descriptionInput = typeof raw?.description === "string" ? raw.description.trim() : ""
  const description = trimText(
    hasMeaningfulDescription(descriptionInput)
      ? descriptionInput
      : buildGeneratedDescription(title || novelTitle, authorName, genres, status),
    1200
  )

  const year = Number(raw?.firstPublishYear)
  const firstPublishYear = Number.isFinite(year) && year >= 1000 && year <= 2100 ? year : undefined

  return {
    title,
    originalTitle,
    authorName,
    originalAuthorName,
    description,
    coverUrl: toOptionalUrl(raw?.coverUrl),
    status,
    genresSuggested: genres,
    firstPublishYear,
    confidence: clampNumber(raw?.confidence, 1, 99, 65),
    source: typeof raw?.source === "string" && raw.source.trim() ? raw.source.trim() : "LLM Web Search",
    sourceUrl: toOptionalUrl(raw?.sourceUrl),
  }
}

function parseResultsFromText(text: string, novelTitle: string): EnrichedItem[] {
  const jsonText = extractJsonCandidate(text)
  if (!jsonText) return []

  let parsed: any = null
  try {
    parsed = JSON.parse(jsonText)
  } catch {
    return []
  }

  const rows = Array.isArray(parsed?.results) ? parsed.results : []
  return rows
    .map((row: any) => toItem(row, novelTitle))
    .filter((row: EnrichedItem | null): row is EnrichedItem => Boolean(row))
    .slice(0, 6)
}

function buildFallbackResult(novelTitle: string): EnrichedItem {
  return {
    title: novelTitle,
    originalTitle: novelTitle,
    authorName: "Chưa rõ",
    originalAuthorName: "Chưa rõ",
    description: buildGeneratedDescription(novelTitle, "Chưa rõ", [], "Đang ra"),
    coverUrl: undefined,
    status: "Đang ra",
    genresSuggested: [],
    firstPublishYear: undefined,
    confidence: 30,
    source: "Fallback",
    sourceUrl: undefined,
  }
}

function buildPrompt(novel: {
  title: string
  originalTitle?: string | null
  authorName: string
  originalAuthorName?: string | null
  description: string
  genres: string[]
}) {
  const shortDescription = trimText(novel.description || "", 1500)

  return [
    "Bạn là trợ lý biên tập truyện dịch từ truyện mạng Trung Quốc.",
    "Hãy tìm thông tin trên web, ưu tiên các nguồn như Qidian, JJWXC, NovelUpdates, wiki fan và nhà xuất bản.",
    "Trả về JSON thuần, không markdown, không giải thích thêm.",
    "Bắt buộc viết các trường title, authorName, description, genresSuggested bằng tiếng Việt có dấu nếu đó là tên hoặc khái niệm đã có bản Việt hóa phổ biến.",
    "Riêng originalTitle và originalAuthorName hãy giữ theo nguyên tác nếu tìm được.",
    "Trường description là bắt buộc. Nếu 'Mô tả hiện tại' (dưới đây) đã dài và chi tiết, hãy giữ nguyên ý chính gốc và chỉ chỉnh sửa văn phong, bố cục cho mượt mà hoặc hấp dẫn hơn. TUYỆT ĐỐI KHÔNG tóm tắt ngắn đi nếu bản gốc đang đầy đủ.",
    "Nếu 'Mô tả hiện tại' chống không hoặc quá sơ sài, hãy tìm kiếm nội dung về cốt truyện gốc trên mạng và viết một bài giới thiệu dài, chi tiết, bám sát các sự kiện thực tế trong truyện. KHÔNG viết chung chung kiểu 'đầy chông gai và thử thách'.",
    "Trạng thái (status) phải luôn là 1 trong 3 giá trị: Đang ra, Hoàn thành, Tạm ngưng. Mảng genresSuggested chứa tối đa 10 thể loại phù hợp.",
    `Schema: {"results":[{"title":"","originalTitle":"","authorName":"","originalAuthorName":"","description":"","coverUrl":"","status":"Đang ra|Hoàn thành|Tạm ngưng","genresSuggested":[],"firstPublishYear":2020,"confidence":80,"source":"","sourceUrl":""}]}`,
    "Tối đa 3 kết quả, ưu tiên kết quả gần nhất với truyện hiện tại.",
    `Tên hiện tại: ${novel.title}`,
    `Tên gốc hiện tại: ${novel.originalTitle || ""}`,
    `Tác giả hiện tại: ${novel.authorName}`,
    `Tác giả gốc hiện tại: ${novel.originalAuthorName || ""}`,
    `Thể loại hiện tại: ${novel.genres.join(", ")}`,
    `Mô tả hiện tại: ${shortDescription}`,
  ].join(" ")
}

async function fetchJsonWithTimeout<T>(url: string, init: RequestInit, timeoutMs = 25000): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const res = await fetch(url, { ...init, signal: controller.signal, cache: "no-store" })
    if (!res.ok) {
      const text = await res.text().catch(() => "")
      throw new Error(`HTTP ${res.status}: ${text.slice(0, 220)}`)
    }
    return (await res.json()) as T
  } finally {
    clearTimeout(timeout)
  }
}

function summarizeError(error: unknown): string {
  if (!(error instanceof Error)) return "Lỗi không xác định"
  return error.message.slice(0, 220)
}

async function tryGoogle(prompt: string, novelTitle: string, attempts: AttemptStatus[]): Promise<ProviderResult | null> {
  const apiKey = process.env.GOOGLE_AI_KEY?.trim()
  const model = process.env.GOOGLE_AI_MODEL?.trim() || "gemini-2.0-flash"

  if (!apiKey) {
    attempts.push({ provider: "google", model, status: "skipped", message: "Thiếu GOOGLE_AI_KEY" })
    return null
  }

  const startedAt = Date.now()
  try {
    const data = await fetchJsonWithTimeout<GeminiResponse>(
      `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(apiKey)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contents: [{ role: "user", parts: [{ text: prompt }] }],
          tools: [{ googleSearch: {} }],
          generationConfig: {
            temperature: 0.2,
            maxOutputTokens: 1400,
          },
        }),
      },
      22000
    )

    const text = (data.candidates || [])
      .flatMap((candidate) => candidate.content?.parts || [])
      .map((part) => part.text || "")
      .join("\n")
      .trim()

    const results = parseResultsFromText(text, novelTitle)
    if (results.length === 0) {
      console.log("Google parsing failed. Raw text:", text)
      throw new Error("Không có kết quả JSON hợp lệ")
    }

    attempts.push({
      provider: "google",
      model,
      status: "success",
      message: `${results.length} kết quả`,
      latencyMs: Date.now() - startedAt,
    })

    return { provider: "google", model, results }
  } catch (error) {
    console.log("Google fetch error:", error)
    attempts.push({
      provider: "google",
      model,
      status: "failed",
      message: summarizeError(error),
      latencyMs: Date.now() - startedAt,
    })
    return null
  }
}

async function resolveOpenRouterFreeModels(apiKey: string): Promise<string[]> {
  const envModels = (process.env.OPENROUTER_FREE_MODELS || "")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.endsWith(":free"))

  const dynamicModels: string[] = []

  try {
    const data = await fetchJsonWithTimeout<OpenRouterModelsResponse>(
      "https://openrouter.ai/api/v1/models",
      {
        method: "GET",
        headers: {
          Authorization: `Bearer ${apiKey}`,
        },
      },
      12000
    )

    for (const row of data.data || []) {
      const id = (row.id || "").trim()
      if (!id || !id.endsWith(":free")) continue
      dynamicModels.push(id)
    }
  } catch {
    // Ignore model-list errors and keep env list fallback.
  }

  const merged = Array.from(new Set([...envModels, ...dynamicModels]))

  const preferredOrder = [
    "google/gemma-2-9b-it:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "qwen/qwen2.5-7b-instruct:free",
  ]

  merged.sort((a, b) => {
    const ai = preferredOrder.findIndex((item) => item === a)
    const bi = preferredOrder.findIndex((item) => item === b)
    const ar = ai === -1 ? 999 : ai
    const br = bi === -1 ? 999 : bi
    if (ar !== br) return ar - br
    return a.localeCompare(b)
  })

  return merged.slice(0, 10)
}

async function tryOpenRouter(prompt: string, novelTitle: string, attempts: AttemptStatus[]): Promise<ProviderResult | null> {
  const apiKey = process.env.OPENROUTER_KEY?.trim()
  if (!apiKey) {
    attempts.push({ provider: "openrouter", model: "free-chain", status: "skipped", message: "Thiếu OPENROUTER_KEY" })
    return null
  }

  const freeModels = await resolveOpenRouterFreeModels(apiKey)
  if (freeModels.length === 0) {
    attempts.push({ provider: "openrouter", model: "free-chain", status: "skipped", message: "Không có model free khả dụng" })
    return null
  }

  for (const model of freeModels) {
    const startedAt = Date.now()
    try {
      const data = await fetchJsonWithTimeout<ChatResponse>(
        "https://openrouter.ai/api/v1/chat/completions",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${apiKey}`,
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "reader-mod-ai-tool",
          },
          body: JSON.stringify({
            model,
            temperature: 0.2,
            max_tokens: 1400,
            messages: [
              { role: "system", content: "Return only valid JSON." },
              { role: "user", content: prompt },
            ],
          }),
        },
        22000
      )

      const text = data.choices?.[0]?.message?.content?.trim() || ""
      const results = parseResultsFromText(text, novelTitle)
      if (results.length === 0) {
        console.log("OpenRouter parsing failed. Raw text:", text)
        throw new Error("Không có kết quả JSON hợp lệ")
      }

      attempts.push({
        provider: "openrouter",
        model,
        status: "success",
        message: `${results.length} kết quả`,
        latencyMs: Date.now() - startedAt,
      })

      return { provider: "openrouter", model, results }
    } catch (error) {
      console.log("OpenRouter fetch error:", error)
      attempts.push({
        provider: "openrouter",
        model,
        status: "failed",
        message: summarizeError(error),
        latencyMs: Date.now() - startedAt,
      })
    }
  }

  return null
}

async function tryDeepSeek(prompt: string, novelTitle: string, attempts: AttemptStatus[]): Promise<ProviderResult | null> {
  const apiKey = process.env.DEEKSEEK_KEY?.trim() || process.env.DEEPSEEK_KEY?.trim()
  const model = process.env.DEEPSEEK_MODEL?.trim() || "deepseek-chat"

  if (!apiKey) {
    attempts.push({ provider: "deepseek", model, status: "skipped", message: "Thiếu DEEKSEEK_KEY/DEEPSEEK_KEY" })
    return null
  }

  const profiles = [
    { maxTokens: 1300, timeoutMs: 60000 },
    { maxTokens: 900, timeoutMs: 45000 },
  ]

  for (const profile of profiles) {
    const startedAt = Date.now()
    try {
      const data = await fetchJsonWithTimeout<ChatResponse>(
        "https://api.deepseek.com/v1/chat/completions",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${apiKey}`,
          },
          body: JSON.stringify({
            model,
            temperature: 0.2,
            max_tokens: profile.maxTokens,
            response_format: { type: "json_object" },
            messages: [
              { role: "system", content: "You are a helpful assistant. You must output only valid standard JSON object following the prompt schema, without markdown formatting." },
              { role: "user", content: prompt },
            ],
          }),
        },
        profile.timeoutMs
      )

      const text = data.choices?.[0]?.message?.content?.trim() || ""
      const results = parseResultsFromText(text, novelTitle)
      if (results.length === 0) {
        console.log("DeepSeek parsing failed. Raw text:", text)
        throw new Error("Không có kết quả JSON hợp lệ")
      }

      attempts.push({
        provider: "deepseek",
        model,
        status: "success",
        message: `${results.length} kết quả`,
        latencyMs: Date.now() - startedAt,
      })

      return { provider: "deepseek", model, results }
    } catch (error) {
      console.log("DeepSeek fetch error:", error)
      attempts.push({
        provider: "deepseek",
        model,
        status: "failed",
        message: summarizeError(error),
        latencyMs: Date.now() - startedAt,
      })
    }
  }

  return null
}

export async function GET(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Không có quyền truy cập" }, { status: 401 })
  }

  const { searchParams } = new URL(req.url)
  const novelId = searchParams.get("novelId")?.trim() || ""

  if (!novelId) {
    return NextResponse.json({ error: "Thiếu novelId" }, { status: 400 })
  }

  const accessWhere = session.user.role === "ADMIN"
    ? { id: novelId }
    : {
        id: novelId,
        OR: [
          { uploaderId: session.user.id },
          { uploaderId: null },
        ],
      }

  const novel = await prisma.novel.findFirst({
    where: accessWhere,
    select: {
      id: true,
      title: true,
      originalTitle: true,
      slug: true,
      authorName: true,
      originalAuthorName: true,
      description: true,
      coverUrl: true,
      status: true,
      updatedAt: true,
      genres: {
        select: {
          genre: {
            select: {
              name: true,
            },
          },
        },
      },
    },
  })

  if (!novel) {
    return NextResponse.json({ error: "Không tìm thấy truyện hoặc bạn không có quyền truy cập" }, { status: 404 })
  }

  const attempts: AttemptStatus[] = []
  const currentNovel = {
    id: novel.id,
    title: novel.title,
    originalTitle: novel.originalTitle,
    slug: novel.slug,
    authorName: novel.authorName,
    originalAuthorName: novel.originalAuthorName,
    description: novel.description,
    coverUrl: novel.coverUrl,
    status: novel.status,
    updatedAt: novel.updatedAt,
    genres: (novel.genres || []).map((row) => row.genre.name),
  }

  const prompt = buildPrompt(currentNovel)

  // Tạm thời bỏ qua Google Gemini theo yêu cầu của user
  attempts.push({
    provider: "google",
    model: "gemini-2.0-flash",
    status: "skipped",
    message: "Tạm bỏ qua theo yêu cầu người dùng.",
  })
  /*
  const google = await tryGoogle(prompt, currentNovel.title, attempts)
  if (google) {
    return NextResponse.json({
      novel: currentNovel,
      provider: google.provider,
      model: google.model,
      attempts,
      count: google.results.length,
      results: google.results,
    })
  }
  */

  const deepseek = await tryDeepSeek(prompt, currentNovel.title, attempts)
  if (deepseek) {
    return NextResponse.json({
      novel: currentNovel,
      provider: deepseek.provider,
      model: deepseek.model,
      attempts,
      count: deepseek.results.length,
      results: deepseek.results,
    })
  }

  if (OPENROUTER_PAUSED) {
    attempts.push({
      provider: "openrouter",
      model: "free-chain",
      status: "skipped",
      message: "Tạm dừng OpenRouter theo cấu hình",
    })
  } else {
    const openrouter = await tryOpenRouter(prompt, currentNovel.title, attempts)
    if (openrouter) {
      return NextResponse.json({
        novel: currentNovel,
        provider: openrouter.provider,
        model: openrouter.model,
        attempts,
        count: openrouter.results.length,
        results: openrouter.results,
      })
    }
  }

  return NextResponse.json({
    novel: currentNovel,
    provider: "fallback",
    model: "none",
    attempts,
    count: 1,
    results: [buildFallbackResult(currentNovel.title)],
    warning: "Google AI và DeepSeek đều thất bại",
  })
}
