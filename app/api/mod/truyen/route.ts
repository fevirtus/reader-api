import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"
import { slugify } from "@/lib/utils"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"
import { deleteR2ObjectByUrl } from "@/lib/r2"

function normalizeOptionalText(value: any): string {
    return typeof value === "string" ? value.trim() : ""
}

async function resolveSeriesIdForWrite(
    seriesIdInput: any,
    seriesNameInput: any,
    userRole: "USER" | "MOD" | "ADMIN",
    userId: string
): Promise<string | null> {
    const seriesId = normalizeOptionalText(seriesIdInput)
    const seriesName = normalizeOptionalText(seriesNameInput)

    if (seriesId) {
        const series = await prisma.series.findFirst({
            where: userRole === "ADMIN"
                ? { id: seriesId }
                : {
                    id: seriesId,
                    OR: [
                        { novels: { some: { uploaderId: userId } } },
                        { novels: { some: { uploaderId: null } } },
                        { novels: { none: {} } },
                    ],
                },
            select: { id: true },
        })

        if (!series) {
            throw new Error("Series không tồn tại hoặc bạn không có quyền sử dụng")
        }

        return series.id
    }

    if (!seriesName) return null

    const existingSeries = await prisma.series.findFirst({
        where: { name: { equals: seriesName, mode: "insensitive" } },
        select: { id: true },
    })

    if (existingSeries) {
        return existingSeries.id
    }

    const baseSlug = slugify(seriesName)
    let slug = baseSlug
    let counter = 1

    while (await prisma.series.findUnique({ where: { slug } })) {
        slug = `${baseSlug}-${counter}`
        counter += 1
    }

    const createdSeries = await prisma.series.create({
        data: {
            name: seriesName,
            slug,
        },
        select: { id: true },
    })

    return createdSeries.id
}

export async function GET() {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const novels = await prisma.novel.findMany({
            where: session.user.role === "ADMIN"
                ? undefined
                : {
                    OR: [
                        { uploaderId: session.user.id },
                        { uploaderId: null },
                    ],
                },
            include: {
                series: {
                    select: { id: true, name: true, slug: true }
                }
            },
            orderBy: { updatedAt: "desc" },
        })
        return NextResponse.json(novels)
    } catch (error) {
        return NextResponse.json({ error: "Failed to fetch novels" }, { status: 500 })
    }
}

export async function POST(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const data = await req.json()
        const { title, originalTitle, authorName, originalAuthorName, description, coverUrl, genreIds = [] } = data
        const seriesId = await resolveSeriesIdForWrite(data?.seriesId, data?.seriesName, session.user.role, session.user.id)
        // Tạo slug từ title
        const slug = slugify(title)

        const newNovel = await prisma.novel.create({
            data: {
                title,
                originalTitle,
                slug: slug,
                authorName,
                originalAuthorName,
                description,
                coverUrl,
                seriesId,
                uploaderId: session.user.id,
                genres: {
                    create: genreIds.map((id: string) => ({
                        genre: { connect: { id } }
                    }))
                }
            },
        })
        return NextResponse.json(newNovel, { status: 201 })
    } catch (error) {
        return NextResponse.json({ error: "Failed to create novel" }, { status: 500 })
    }
}

export async function PUT(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const data = await req.json()
        const { id, title, originalTitle, authorName, originalAuthorName, description, coverUrl, status, genreIds } = data

        if (!id) {
            return NextResponse.json({ error: "Thiếu ID truyện" }, { status: 400 })
        }

        const hasField = (field: string) => Object.prototype.hasOwnProperty.call(data, field)

        const targetNovel = await prisma.novel.findFirst({
            where: session.user.role === "ADMIN"
                ? { id }
                : {
                    id,
                    OR: [
                        { uploaderId: session.user.id },
                        { uploaderId: null },
                    ],
                },
            select: { id: true, seriesId: true },
        })

        if (!targetNovel) {
            return NextResponse.json({ error: "Truyện không tồn tại hoặc không đủ quyền" }, { status: 404 })
        }

        // Disable editing series relation from novel edit form: keep current seriesId.
        const fixedSeriesId = targetNovel.seriesId

        if (fixedSeriesId) {
            const sharedData: Record<string, unknown> = {}
            if (hasField("originalTitle")) sharedData.originalTitle = originalTitle
            if (hasField("authorName")) sharedData.authorName = authorName
            if (hasField("originalAuthorName")) sharedData.originalAuthorName = originalAuthorName
            if (hasField("description")) sharedData.description = description
            if (hasField("status")) sharedData.status = status

            const ownData: Record<string, unknown> = {}
            if (hasField("title")) ownData.title = title
            if (hasField("coverUrl")) ownData.coverUrl = coverUrl
            if (session.user.role === "MOD") ownData.uploaderId = session.user.id

            const seriesNovels = await prisma.novel.findMany({
                where: { seriesId: fixedSeriesId },
                select: { id: true },
            })
            const seriesNovelIds = seriesNovels.map((novel) => novel.id)

            const updatedNovel = await prisma.$transaction(async (tx) => {
                // Sync shared metadata for all novels in the same series.
                if (Object.keys(sharedData).length > 0) {
                    await tx.novel.updateMany({
                        where: { id: { in: seriesNovelIds } },
                        data: sharedData,
                    })
                }

                if (genreIds !== undefined) {
                    await tx.novelGenre.deleteMany({
                        where: { novelId: { in: seriesNovelIds } },
                    })

                    if (genreIds.length > 0) {
                        await tx.novelGenre.createMany({
                            data: seriesNovelIds.flatMap((novelId) =>
                                genreIds.map((genreId: string) => ({ novelId, genreId }))
                            ),
                        })
                    }
                }

                // Only current novel keeps its own title and cover.
                if (Object.keys(ownData).length === 0) {
                    return tx.novel.findUnique({ where: { id } })
                }

                return tx.novel.update({
                    where: { id },
                    data: ownData,
                })
            })

            return NextResponse.json(updatedNovel)
        }

        const updateData: Record<string, unknown> = {
            seriesId: fixedSeriesId,
            ...(session.user.role === "MOD" && { uploaderId: session.user.id }),
        }

        if (hasField("title")) updateData.title = title
        if (hasField("originalTitle")) updateData.originalTitle = originalTitle
        if (hasField("authorName")) updateData.authorName = authorName
        if (hasField("originalAuthorName")) updateData.originalAuthorName = originalAuthorName
        if (hasField("description")) updateData.description = description
        if (hasField("coverUrl")) updateData.coverUrl = coverUrl
        if (hasField("status")) updateData.status = status

        const updatedNovel = await prisma.novel.update({
            where: { id },
            data: {
                ...updateData,
                ...(genreIds !== undefined && {
                    genres: {
                        deleteMany: {},
                        create: genreIds.map((gId: string) => ({
                            genre: { connect: { id: gId } }
                        }))
                    }
                })
            },
        })

        return NextResponse.json(updatedNovel)
    } catch (error) {
        return NextResponse.json({ error: "Failed to update novel" }, { status: 500 })
    }
}

export async function DELETE(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const url = new URL(req.url)
        const id = url.searchParams.get("id")

        if (!id) return NextResponse.json({ error: "Thiếu ID truyện" }, { status: 400 })

        const novel = await prisma.novel.findFirst({
            where: session.user.role === "ADMIN"
                ? { id }
                : {
                    id,
                    OR: [
                        { uploaderId: session.user.id },
                        { uploaderId: null },
                    ],
                },
            select: { id: true, coverUrl: true, seriesId: true }
        })

        if (!novel) {
            return NextResponse.json({ error: "Truyện không tồn tại hoặc không đủ quyền" }, { status: 404 })
        }

        await connectToMongoDB()
        const chapterDeleteResult = await Chapter.deleteMany({ novelId: id })

        await prisma.novel.delete({
            where: { id },
        })

        await deleteR2ObjectByUrl(novel.coverUrl).catch(() => { })

        if (novel.seriesId) {
            const remainingSeriesNovels = await prisma.novel.count({ where: { seriesId: novel.seriesId } })
            if (remainingSeriesNovels === 0) {
                await prisma.series.delete({ where: { id: novel.seriesId } }).catch(() => { })
            }
        }

        return NextResponse.json({
            message: "Đã xóa truyện và toàn bộ chương thành công",
            deletedChapters: chapterDeleteResult.deletedCount || 0
        })
    } catch (error) {
        return NextResponse.json({ error: "Failed to delete novel" }, { status: 500 })
    }
}
