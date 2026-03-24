import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

export async function GET(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Không có quyền truy cập" }, { status: 401 })
  }

  const { searchParams } = new URL(req.url)
  const q = (searchParams.get("q") || "").trim()
  const page = parseInt(searchParams.get("page") || "1", 10)
  const take = 24
  const skip = (Math.max(1, page) - 1) * take

  const whereScope = session.user.role === "ADMIN"
    ? {}
    : {
        OR: [
          { uploaderId: session.user.id },
          { uploaderId: null },
        ],
      }

  const isMissingFilter = searchParams.get("missing") === "true"
  const baseWhereOptions: any[] = [whereScope]

  if (q.length > 0) {
    baseWhereOptions.push({
      OR: [
        { title: { contains: q, mode: "insensitive" } },
        { originalTitle: { contains: q, mode: "insensitive" } },
        { authorName: { contains: q, mode: "insensitive" } },
        { originalAuthorName: { contains: q, mode: "insensitive" } },
        { slug: { contains: q, mode: "insensitive" } },
      ],
    })
  } else if (!isMissingFilter) {
      return NextResponse.json({ novels: [], hasMore: false })
  }

  if (isMissingFilter) {
    baseWhereOptions.push({
      OR: [
        { authorName: { in: ["", "Chưa rõ"] } },
        { description: "" },
        { description: { in: ["Chưa có giới thiệu", "Không có giới thiệu", "chưa có giới thiệu", "không có", "chưa rõ", "đang cập nhật"] } }
      ],
    })
  }

  const rows = await prisma.novel.findMany({
    where: {
      AND: baseWhereOptions,
    },
    select: {
      id: true,
      title: true,
      originalTitle: true,
      slug: true,
      authorName: true,
      originalAuthorName: true,
      description: true,
      coverUrl: true,
      status: true,
      updatedAt: true,
    },
    orderBy: [{ updatedAt: "desc" }],
    take: take + 1, // Fetch one extra to know if there's a next page
    skip,
  })

  const hasMore = rows.length > take
  const returnRows = hasMore ? rows.slice(0, take) : rows

  return NextResponse.json({ novels: returnRows, hasMore })
}
