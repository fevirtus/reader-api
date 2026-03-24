import { NextResponse } from "next/server"
import { prisma } from "@/lib/prisma"

export async function GET(req: Request) {
  try {
    const url = new URL(req.url)
    const q = url.searchParams.get("q")?.trim() || ""

    if (q.length < 2) {
      return NextResponse.json([])
    }

    const novels = await prisma.novel.findMany({
      where: {
        OR: [
          { title: { contains: q, mode: "insensitive" } },
          { authorName: { contains: q, mode: "insensitive" } },
          { series: { name: { contains: q, mode: "insensitive" } } },
        ],
      },
      select: {
        id: true,
        title: true,
        slug: true,
        authorName: true,
        coverUrl: true,
        series: {
          select: {
            id: true,
            name: true,
          },
        },
      },
      orderBy: [{ views: "desc" }, { updatedAt: "desc" }],
      take: 8,
    })

    return NextResponse.json(novels)
  } catch {
    return NextResponse.json({ error: "Failed to fetch suggestions" }, { status: 500 })
  }
}
