import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"
import { prisma } from "@/lib/prisma"

export async function GET(
    req: Request,
    context: { params: Promise<{ id: string }> }
) {
    const { id } = await context.params
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        await connectToMongoDB()
        // console.log("Fetching chapter with ID:", id)

        const chapter = await Chapter.findById(id)

        if (!chapter) {
            // console.log("Chapter not found in DB")
            return NextResponse.json({ error: "Chapter not found" }, { status: 404 })
        }

        // Verify the moderator owns the related novel (or is an ADMIN)
        let novelQuery: any = { id: chapter.novelId }
        if (session.user.role !== "ADMIN") {
            novelQuery.uploaderId = session.user.id
        }

        const novel = await prisma.novel.findFirst({
            where: novelQuery
        })

        if (!novel) {
            console.log("Novel not found or unauthorized:", {
                chapterNovelId: chapter.novelId,
                userId: session.user.id,
                role: session.user.role
            })
            return NextResponse.json({ error: "Unauthorized access to this chapter" }, { status: 403 })
        }

        return NextResponse.json(chapter)
    } catch (error) {
        console.error("GET Chapter error:", error)
        return NextResponse.json({ error: "Failed to fetch chapter details" }, { status: 500 })
    }
}
