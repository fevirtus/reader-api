import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

export async function GET(req: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id: novelId } = await params
    const { searchParams } = new URL(req.url)
    const chapterId = searchParams.get("chapterId") || undefined
    const page = Math.max(1, parseInt(searchParams.get("page") || "1", 10))
    const limit = Math.min(50, Math.max(1, parseInt(searchParams.get("limit") || "20", 10)))
    const skip = (page - 1) * limit

    const where: Record<string, any> = { novelId }
    if (chapterId) {
      where.chapterId = chapterId
    } else {
      where.chapterId = null
    }

    const [comments, totalCount] = await Promise.all([
      prisma.comment.findMany({
        where,
        orderBy: { createdAt: "desc" },
        skip,
        take: limit,
        include: { user: { select: { id: true, name: true, image: true } } },
      }),
      prisma.comment.count({ where }),
    ])

    return NextResponse.json({
      comments: comments.map((c) => ({
        id: c.id,
        userId: c.userId,
        username: c.user.name || "User",
        avatarUrl: c.user.image || null,
        novelId: c.novelId,
        chapterId: c.chapterId,
        content: c.content,
        createdAt: c.createdAt.toISOString(),
      })),
      totalCount,
      totalPages: Math.ceil(totalCount / limit),
      currentPage: page,
    })
  } catch (error) {
    console.error("GET Comments Error", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}

export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
    try {
        const session = await getServerSession(authOptions)
        if (!session?.user?.id) {
            return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
        }

        const { id: novelId } = await params
        const body = await req.json()
        const { content, chapterId } = body

        if (!content || typeof content !== "string") {
            return NextResponse.json({ error: "Content is required" }, { status: 400 })
        }

        const newComment = await prisma.comment.create({
            data: {
                content: content.trim(),
                userId: session.user.id,
                novelId,
                chapterId: chapterId || null
            },
            include: {
                user: true
            }
        })

        return NextResponse.json({
            id: newComment.id,
            userId: newComment.user.id,
            username: newComment.user.name || "User",
            avatarColor: newComment.user.image || "bg-primary",
            novelId: newComment.novelId,
            chapterId: newComment.chapterId,
            content: newComment.content,
            createdAt: newComment.createdAt.toISOString().split("T")[0]
        })
    } catch (error) {
        console.error("POST Comment Error", error)
        return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
    }
}
