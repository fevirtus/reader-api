import { NextResponse } from "next/server"
import { prisma } from "@/lib/prisma"

export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
    try {
        const { id } = await params
        const body = await req.json()
        const { score } = body

        if (typeof score !== 'number' || score < 1 || score > 5) {
            return NextResponse.json({ error: "Invalid score" }, { status: 400 })
        }

        // Fetch current rating
        const novel = await prisma.novel.findUnique({
            where: { id },
            select: { rating: true, ratingCount: true }
        })

        if (!novel) {
            return NextResponse.json({ error: "Novel not found" }, { status: 404 })
        }

        // Atomic increment using raw SQL to avoid race conditions
        const updatedNovel = await prisma.$queryRaw<{ rating: number; ratingCount: number }[]>`
          UPDATE "Novel"
          SET "ratingCount" = "ratingCount" + 1,
              "rating"      = (("rating" * "ratingCount") + ${score}) / ("ratingCount" + 1)
          WHERE id = ${id}
          RETURNING rating, "ratingCount"
        `.then((rows) => rows[0])

        return NextResponse.json({
            rating: updatedNovel.rating,
            ratingCount: updatedNovel.ratingCount
        })
    } catch (error) {
        console.error("Rating Error", error)
        return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
    }
}
