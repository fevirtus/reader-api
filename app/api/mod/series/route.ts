import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import { slugify } from "@/lib/utils"

function normalizeText(value: any): string {
  return typeof value === "string" ? value.trim() : ""
}

async function resolveEditableSeries(
  id: string,
  session: { user: { role: "USER" | "MOD" | "ADMIN"; id: string } }
) {
  return prisma.series.findFirst({
    where: session.user.role === "ADMIN"
      ? { id }
      : {
          id,
          OR: [
            { novels: { some: { uploaderId: session.user.id } } },
            { novels: { some: { uploaderId: null } } },
            { novels: { none: {} } },
          ],
        },
    select: { id: true },
  })
}

export async function GET() {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const series = await prisma.series.findMany({
      where: session.user.role === "ADMIN"
        ? undefined
        : {
            OR: [
              { novels: { some: { uploaderId: session.user.id } } },
              { novels: { some: { uploaderId: null } } },
              { novels: { none: {} } },
            ],
          },
      orderBy: { updatedAt: "desc" },
      select: {
        id: true,
        name: true,
        slug: true,
        description: true,
        _count: { select: { novels: true } },
      },
    })

    return NextResponse.json(series)
  } catch {
    return NextResponse.json({ error: "Failed to fetch series" }, { status: 500 })
  }
}

export async function POST(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const body = await req.json()
    const name = normalizeText(body?.name)
    const description = normalizeText(body?.description)

    if (!name) {
      return NextResponse.json({ error: "Tên series không được để trống" }, { status: 400 })
    }

    const existing = await prisma.series.findFirst({
      where: { name: { equals: name, mode: "insensitive" } },
      select: { id: true, name: true, slug: true, description: true },
    })

    if (existing) {
      return NextResponse.json(existing)
    }

    const baseSlug = slugify(name)
    let slug = baseSlug
    let counter = 1

    while (await prisma.series.findUnique({ where: { slug } })) {
      slug = `${baseSlug}-${counter}`
      counter += 1
    }

    const created = await prisma.series.create({
      data: { name, slug, description: description || null },
      select: { id: true, name: true, slug: true, description: true },
    })

    return NextResponse.json(created, { status: 201 })
  } catch {
    return NextResponse.json({ error: "Failed to create series" }, { status: 500 })
  }
}

export async function PUT(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const body = await req.json()
    const id = normalizeText(body?.id)
    const name = normalizeText(body?.name)
    const description = normalizeText(body?.description)

    if (!id || !name) {
      return NextResponse.json({ error: "Thiếu thông tin series" }, { status: 400 })
    }

    const target = await resolveEditableSeries(id, session as any)
    if (!target) {
      return NextResponse.json({ error: "Không tìm thấy series hoặc không đủ quyền" }, { status: 404 })
    }

    const duplicated = await prisma.series.findFirst({
      where: {
        id: { not: id },
        name: { equals: name, mode: "insensitive" },
      },
      select: { id: true },
    })

    if (duplicated) {
      return NextResponse.json({ error: "Tên series đã tồn tại" }, { status: 409 })
    }

    const baseSlug = slugify(name)
    let slug = baseSlug
    let counter = 1

    while (await prisma.series.findFirst({ where: { slug, id: { not: id } }, select: { id: true } })) {
      slug = `${baseSlug}-${counter}`
      counter += 1
    }

    const updated = await prisma.series.update({
      where: { id },
      data: {
        name,
        slug,
        description: description || null,
      },
      select: {
        id: true,
        name: true,
        slug: true,
        description: true,
        _count: { select: { novels: true } },
      },
    })

    return NextResponse.json(updated)
  } catch {
    return NextResponse.json({ error: "Failed to update series" }, { status: 500 })
  }
}

export async function DELETE(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }

  try {
    const url = new URL(req.url)
    const id = normalizeText(url.searchParams.get("id"))

    if (!id) {
      return NextResponse.json({ error: "Thiếu id series" }, { status: 400 })
    }

    const target = await resolveEditableSeries(id, session as any)
    if (!target) {
      return NextResponse.json({ error: "Không tìm thấy series hoặc không đủ quyền" }, { status: 404 })
    }

    const usedCount = await prisma.novel.count({ where: { seriesId: id } })
    if (usedCount > 0) {
      return NextResponse.json({ error: "Series đang chứa truyện, không thể xóa" }, { status: 409 })
    }

    await prisma.series.delete({ where: { id } })
    return NextResponse.json({ success: true })
  } catch {
    return NextResponse.json({ error: "Failed to delete series" }, { status: 500 })
  }
}
