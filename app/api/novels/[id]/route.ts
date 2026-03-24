import { NextResponse } from "next/server"
import { prisma } from "@/lib/prisma"

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const { id } = await params

    // Support both id and slug
    const novel = await prisma.novel.findFirst({
      where: { OR: [{ id }, { slug: id }] },
      select: {
        id: true,
        title: true,
        slug: true,
        originalTitle: true,
        authorName: true,
        originalAuthorName: true,
        description: true,
        coverUrl: true,
        coverColor: true,
        status: true,
        totalChapters: true,
        views: true,
        rating: true,
        ratingCount: true,
        bookmarkCount: true,
        seriesId: true,
        series: {
          select: {
            id: true,
            name: true,
            slug: true,
            novels: {
              select: {
                id: true,
                title: true,
                slug: true,
                totalChapters: true,
                status: true,
                coverUrl: true,
              },
              orderBy: { title: "asc" },
            },
          },
        },
        genres: { select: { genre: { select: { id: true, name: true, slug: true } } } },
        createdAt: true,
        updatedAt: true,
      },
    })

    if (!novel) {
      return NextResponse.json({ error: "Novel not found" }, { status: 404 })
    }

    return NextResponse.json({
      ...novel,
      genres: novel.genres.map((g) => g.genre),
    })
  } catch (error) {
    console.error("Novel detail error:", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}
