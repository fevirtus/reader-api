import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import connectToMongoDB from "@/lib/mongoose"
import { EditorRecommendation } from "@/lib/models/editor-recommendation"

const MAX_RECOMMENDATIONS_PER_EDITOR = 5

function normalizeText(value: any): string {
  return typeof value === "string" ? value.trim() : ""
}

function isAllowedModerator(role: string) {
  return role === "MOD" || role === "ADMIN"
}

export async function GET(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || !isAllowedModerator(session.user.role)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const url = new URL(req.url)
    const q = normalizeText(url.searchParams.get("q"))

    await connectToMongoDB()

    const docs = (await EditorRecommendation.find({})
      .sort({ createdAt: -1 })
      .limit(1000)
      .lean()) as Array<{
      _id: any
      novelId: string
      editorId: string
      createdAt?: Date
    }>

    const novelIds = Array.from(new Set(docs.map((doc) => doc.novelId).filter(Boolean)))
    const editorIds = Array.from(new Set(docs.map((doc) => doc.editorId).filter(Boolean)))

    const [novels, editors] = await Promise.all([
      novelIds.length > 0
        ? prisma.novel.findMany({
            where: { id: { in: novelIds } },
            select: {
              id: true,
              title: true,
              slug: true,
              authorName: true,
              coverUrl: true,
              status: true,
              totalChapters: true,
            },
          })
        : Promise.resolve([]),
      editorIds.length > 0
        ? prisma.user.findMany({
            where: { id: { in: editorIds } },
            select: { id: true, name: true },
          })
        : Promise.resolve([]),
    ])

    const novelMap = new Map(novels.map((novel) => [novel.id, novel]))
    const editorMap = new Map(editors.map((editor) => [editor.id, editor]))

    const recommendationCountMap = new Map<string, number>()
    for (const doc of docs) {
      recommendationCountMap.set(doc.novelId, (recommendationCountMap.get(doc.novelId) || 0) + 1)
    }

    const items = docs
      .map((doc) => {
        const novel = novelMap.get(doc.novelId)
        if (!novel) return null

        const editor = editorMap.get(doc.editorId)
        return {
          id: String(doc._id),
          createdAt: doc.createdAt || null,
          recommendCount: recommendationCountMap.get(doc.novelId) || 0,
          novel,
          editor: {
            id: doc.editorId,
            name: editor?.name || "Biên tập viên",
          },
        }
      })
      .filter((item): item is NonNullable<typeof item> => Boolean(item))

    const summary = Array.from(recommendationCountMap.entries())
      .map(([novelId, recommendCount]) => {
        const novel = novelMap.get(novelId)
        if (!novel) return null
        return { novel, recommendCount }
      })
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .sort((a, b) => b.recommendCount - a.recommendCount)

    const myNovelIdSet = new Set(
      docs.filter((doc) => doc.editorId === session.user.id).map((doc) => doc.novelId)
    )
    const myRecommendationCount = myNovelIdSet.size

    const candidates = q
      ? await prisma.novel.findMany({
          where: {
            OR: [
              { title: { contains: q, mode: "insensitive" } },
              { slug: { contains: q, mode: "insensitive" } },
              { authorName: { contains: q, mode: "insensitive" } },
            ],
          },
          select: {
            id: true,
            title: true,
            slug: true,
            authorName: true,
            coverUrl: true,
            status: true,
            totalChapters: true,
          },
          take: 20,
          orderBy: [{ updatedAt: "desc" }],
        })
      : []

    const candidateRows = candidates.map((novel) => ({
      ...novel,
      alreadyRecommended: myNovelIdSet.has(novel.id),
      recommendCount: recommendationCountMap.get(novel.id) || 0,
    }))

    return NextResponse.json({
      items,
      summary,
      candidates: candidateRows,
      myNovelIds: Array.from(myNovelIdSet),
      currentUser: {
        id: session.user.id,
        role: session.user.role,
        recommendationCount: myRecommendationCount,
        maxRecommendationCount: MAX_RECOMMENDATIONS_PER_EDITOR,
      },
    })
  } catch (error) {
    console.error("Failed to fetch editor recommendations", error)
    return NextResponse.json({ error: "Failed to fetch recommendations" }, { status: 500 })
  }
}

export async function POST(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || !isAllowedModerator(session.user.role)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const body = await req.json()
    const novelId = normalizeText(body?.novelId)

    if (!novelId) {
      return NextResponse.json({ error: "Thiếu ID truyện" }, { status: 400 })
    }

    const existedNovel = await prisma.novel.findUnique({
      where: { id: novelId },
      select: { id: true },
    })

    if (!existedNovel) {
      return NextResponse.json({ error: "Truyện không tồn tại" }, { status: 404 })
    }

    await connectToMongoDB()

    const existing = (await EditorRecommendation.findOne({
      novelId,
      editorId: session.user.id,
    })
      .select({ _id: 1 })
      .lean()) as { _id: any } | null

    if (existing) {
      return NextResponse.json({ error: "Bạn đã đề cử truyện này rồi" }, { status: 409 })
    }

    const myRecommendationCount = await EditorRecommendation.countDocuments({
      editorId: session.user.id,
    })

    if (myRecommendationCount >= MAX_RECOMMENDATIONS_PER_EDITOR) {
      return NextResponse.json(
        { error: `Mỗi biên tập viên chỉ được đề cử tối đa ${MAX_RECOMMENDATIONS_PER_EDITOR} truyện` },
        { status: 400 }
      )
    }

    try {
      const created = await EditorRecommendation.create({
        novelId,
        editorId: session.user.id,
      })

      return NextResponse.json(
        {
          id: String(created._id),
          novelId,
          editorId: session.user.id,
        },
        { status: 201 }
      )
    } catch (error: any) {
      if (error?.code === 11000) {
        return NextResponse.json({ error: "Bạn đã đề cử truyện này rồi" }, { status: 409 })
      }
      throw error
    }
  } catch (error) {
    console.error("Failed to create editor recommendation", error)
    return NextResponse.json({ error: "Failed to create recommendation" }, { status: 500 })
  }
}

export async function DELETE(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || !isAllowedModerator(session.user.role)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const url = new URL(req.url)
    const id = normalizeText(url.searchParams.get("id"))

    if (!id) {
      return NextResponse.json({ error: "Thiếu ID đề cử" }, { status: 400 })
    }

    await connectToMongoDB()

    const existed = (await EditorRecommendation.findById(id).lean()) as {
      _id: any
      editorId: string
    } | null

    if (!existed) {
      return NextResponse.json({ error: "Đề cử không tồn tại" }, { status: 404 })
    }

    if (session.user.role !== "ADMIN" && existed.editorId !== session.user.id) {
      return NextResponse.json({ error: "Bạn không thể xóa đề cử của người khác" }, { status: 403 })
    }

    await EditorRecommendation.deleteOne({ _id: id })
    return NextResponse.json({ success: true })
  } catch (error) {
    console.error("Failed to delete editor recommendation", error)
    return NextResponse.json({ error: "Failed to delete recommendation" }, { status: 500 })
  }
}
