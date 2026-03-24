import { NextResponse } from "next/server"
import { prisma } from "@/lib/prisma"

export async function GET() {
  try {
    const genres = await prisma.genre.findMany({
      orderBy: { name: "asc" },
      select: {
        id: true,
        name: true,
        slug: true,
        description: true,
        icon: true,
        _count: { select: { novels: true } },
      },
    })

    return NextResponse.json(
      genres.map((g) => ({
        id: g.id,
        name: g.name,
        slug: g.slug,
        description: g.description,
        icon: g.icon,
        novelCount: g._count.novels,
      }))
    )
  } catch (error) {
    console.error("Genres list error:", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}
