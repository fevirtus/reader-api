import { NextResponse } from "next/server"
import { prisma } from "@/lib/prisma"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"

type SortKey = "latest" | "popular" | "rating" | "name"

export async function GET(req: Request) {
  try {
    const { searchParams } = new URL(req.url)
    const q = searchParams.get("q")?.trim() || ""
    const genre = searchParams.get("genre") || ""
    const status = searchParams.get("status") || ""
    const sort: SortKey = (searchParams.get("sort") as SortKey) || "latest"
    const page = Math.max(1, parseInt(searchParams.get("page") || "1", 10))
    const limit = Math.min(100, Math.max(1, parseInt(searchParams.get("limit") || "20", 10)))
    const skip = (page - 1) * limit

    // Build where clause
    const where: Record<string, any> = {}
    if (status) where.status = status
    if (genre) {
      where.genres = { some: { genre: { slug: genre } } }
    }
    if (q) {
      where.OR = [
        { title: { contains: q, mode: "insensitive" } },
        { originalTitle: { contains: q, mode: "insensitive" } },
        { authorName: { contains: q, mode: "insensitive" } },
        { originalAuthorName: { contains: q, mode: "insensitive" } },
        { series: { name: { contains: q, mode: "insensitive" } } },
      ]
    }

    // Build orderBy
    const orderBy: Record<string, any> =
      sort === "popular"
        ? { views: "desc" }
        : sort === "rating"
        ? { rating: "desc" }
        : sort === "name"
        ? { title: "asc" }
        : { updatedAt: "desc" }

    const [novels, totalCount] = await Promise.all([
      prisma.novel.findMany({
        where,
        orderBy,
        skip,
        take: limit,
        select: {
          id: true,
          title: true,
          slug: true,
          originalTitle: true,
          authorName: true,
          coverUrl: true,
          coverColor: true,
          status: true,
          totalChapters: true,
          views: true,
          rating: true,
          ratingCount: true,
          bookmarkCount: true,
          seriesId: true,
          series: { select: { id: true, name: true, slug: true } },
          genres: { select: { genre: { select: { id: true, name: true, slug: true } } } },
          updatedAt: true,
        },
      }),
      prisma.novel.count({ where }),
    ])

    // Attach latest chapter info
    await connectToMongoDB()
    const novelIds = novels.map((n) => n.id)
    const latestChapters = await Chapter.aggregate([
      { $match: { novelId: { $in: novelIds } } },
      { $sort: { novelId: 1, number: -1 } },
      {
        $group: {
          _id: "$novelId",
          latestChapterNumber: { $first: "$number" },
          latestChapterTitle: { $first: "$title" },
          latestChapterAt: { $first: "$createdAt" },
        },
      },
    ])
    const chapterMap = Object.fromEntries(
      latestChapters.map((c) => [c._id, c])
    )

    const items = novels.map((n) => ({
      ...n,
      genres: n.genres.map((g) => g.genre),
      latestChapter: chapterMap[n.id]
        ? {
            number: chapterMap[n.id].latestChapterNumber,
            title: chapterMap[n.id].latestChapterTitle,
            createdAt: chapterMap[n.id].latestChapterAt,
          }
        : null,
    }))

    return NextResponse.json({
      items,
      totalCount,
      totalPages: Math.ceil(totalCount / limit),
      currentPage: page,
    })
  } catch (error) {
    console.error("Browse novels error:", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}
