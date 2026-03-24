import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"
import { deleteR2ObjectByUrl } from "@/lib/r2"

function normalizeIds(value: any): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item) => typeof item === "string" && item.trim()).map((item) => item.trim())
}

export async function POST(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const body = await req.json()
    const action = typeof body?.action === "string" ? body.action : ""
    const ids = normalizeIds(body?.ids)

    if (ids.length === 0) {
      return NextResponse.json({ error: "Danh sách truyện trống" }, { status: 400 })
    }

    const accessibleNovels = await prisma.novel.findMany({
      where: session.user.role === "ADMIN"
        ? { id: { in: ids } }
        : {
            id: { in: ids },
            OR: [
              { uploaderId: session.user.id },
              { uploaderId: null },
            ],
          },
      select: {
        id: true,
        coverUrl: true,
      },
    })

    if (accessibleNovels.length === 0) {
      return NextResponse.json({ error: "Không có truyện hợp lệ để thao tác" }, { status: 404 })
    }

    const accessibleIds = accessibleNovels.map((novel) => novel.id)

    if (action === "delete") {
      await connectToMongoDB()
      await Chapter.deleteMany({ novelId: { $in: accessibleIds } })

      await prisma.novel.deleteMany({
        where: { id: { in: accessibleIds } },
      })

      await Promise.all(
        accessibleNovels.map((novel) => deleteR2ObjectByUrl(novel.coverUrl).catch(() => {}))
      )

      return NextResponse.json({ success: true, deletedCount: accessibleIds.length })
    }

    return NextResponse.json({ error: "Chỉ hỗ trợ xóa hàng loạt" }, { status: 400 })
  } catch {
    return NextResponse.json({ error: "Bulk operation failed" }, { status: 500 })
  }
}
