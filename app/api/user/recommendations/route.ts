import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import connectToMongoDB from "@/lib/mongoose"
import { UserRecommendation } from "@/lib/models/user-recommendation"

function normalizeText(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}

export async function GET() {
  try {
    const session = await getServerSession(authOptions)
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    await connectToMongoDB()

    const docs = (await UserRecommendation.find({ userId: session.user.id })
      .sort({ createdAt: -1 })
      .limit(1000)
      .lean()) as Array<{
      _id: any
      novelId: string
      createdAt?: Date
    }>

    const novelIds = Array.from(new Set(docs.map((doc) => doc.novelId).filter(Boolean)))
    const novels = novelIds.length
      ? await prisma.novel.findMany({
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
      : []

    const novelMap = new Map(novels.map((novel) => [novel.id, novel]))

    const items = docs
      .map((doc) => {
        const novel = novelMap.get(doc.novelId)
        if (!novel) return null

        return {
          id: String(doc._id),
          novelId: doc.novelId,
          createdAt: doc.createdAt || null,
          novel,
        }
      })
      .filter((item): item is NonNullable<typeof item> => Boolean(item))

    return NextResponse.json(items)
  } catch (error) {
    console.error("Failed to fetch user recommendations", error)
    return NextResponse.json({ error: "Failed to fetch recommendations" }, { status: 500 })
  }
}

export async function POST(req: Request) {
  try {
    const session = await getServerSession(authOptions)
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    const body = await req.json()
    const novelId = normalizeText(body?.novelId)

    if (!novelId) {
      return NextResponse.json({ error: "Thiếu ID truyện" }, { status: 400 })
    }

    const novel = await prisma.novel.findUnique({ where: { id: novelId }, select: { id: true } })
    if (!novel) {
      return NextResponse.json({ error: "Truyện không tồn tại" }, { status: 404 })
    }

    await connectToMongoDB()

    try {
      const existing = (await UserRecommendation.findOne({
        userId: session.user.id,
        novelId,
      })
        .select({ _id: 1 })
        .lean()) as { _id: any } | null

      if (existing) {
        return NextResponse.json({ error: "Bạn đã đề cử truyện này rồi" }, { status: 409 })
      }

      const created = await UserRecommendation.create({
        userId: session.user.id,
        novelId,
      })

      await prisma.novel.update({
        where: { id: novelId },
        data: { bookmarkCount: { increment: 1 } },
      })

      return NextResponse.json(
        {
          id: String(created._id),
          novelId,
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
    console.error("Failed to create user recommendation", error)
    return NextResponse.json({ error: "Failed to create recommendation" }, { status: 500 })
  }
}

export async function DELETE(req: Request) {
  try {
    const session = await getServerSession(authOptions)
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    const url = new URL(req.url)
    const novelId = normalizeText(url.searchParams.get("novelId"))

    if (!novelId) {
      return NextResponse.json({ error: "Thiếu ID truyện" }, { status: 400 })
    }

    await connectToMongoDB()

    const existing = (await UserRecommendation.findOne({
      userId: session.user.id,
      novelId,
    })
      .select({ _id: 1 })
      .lean()) as { _id: any } | null

    if (!existing) {
      return NextResponse.json({ error: "Bạn chưa đề cử truyện này" }, { status: 404 })
    }

    await UserRecommendation.deleteOne({ _id: existing._id })

    await prisma.novel.update({
      where: { id: novelId },
      data: { bookmarkCount: { decrement: 1 } },
    })

    return NextResponse.json({ success: true })
  } catch (error) {
    console.error("Failed to delete user recommendation", error)
    return NextResponse.json({ error: "Failed to delete recommendation" }, { status: 500 })
  }
}
