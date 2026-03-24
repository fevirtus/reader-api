import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

type MissingKey = "author" | "cover" | "description" | "genres"

const ALL_MISSING_KEYS: MissingKey[] = ["author", "cover", "description", "genres"]

function getScopeWhere(session: { user: { role: string; id: string } }) {
    if (session.user.role === "ADMIN") {
        return {}
    }

    return {
        OR: [
            { uploaderId: session.user.id },
            { uploaderId: null },
        ],
    }
}

function parseMissingKeys(raw: string | null): MissingKey[] {
    if (!raw || !raw.trim()) return ALL_MISSING_KEYS

    const parsed = raw
        .split(",")
        .map((item) => item.trim().toLowerCase())
        .filter((item): item is MissingKey => ALL_MISSING_KEYS.includes(item as MissingKey))

    if (parsed.length === 0) return ALL_MISSING_KEYS
    return Array.from(new Set(parsed))
}

function buildMissingWhereForKey(key: MissingKey) {
    switch (key) {
        case "author":
            return { authorName: { equals: "" } }
        case "cover":
            return {
                OR: [
                    { coverUrl: null },
                    { coverUrl: { equals: "" } },
                ],
            }
        case "description":
            return { description: { equals: "" } }
        case "genres":
            return { genres: { none: {} } }
        default:
            return {}
    }
}

function computeMissingStatus(novel: {
    authorName: string
    coverUrl: string | null
    description: string
    genres: Array<{ genre: { id: string; name: string } }>
}) {
    const authorMissing = novel.authorName.trim().length === 0
    const coverMissing = !novel.coverUrl || novel.coverUrl.trim().length === 0
    const descriptionMissing = novel.description.trim().length === 0
    const genresMissing = novel.genres.length === 0

    return {
        author: authorMissing,
        cover: coverMissing,
        description: descriptionMissing,
        genres: genresMissing,
    }
}

function hasSelectedMissing(missingStatus: Record<MissingKey, boolean>, selected: MissingKey[]) {
    return selected.some((key) => missingStatus[key])
}

export async function GET(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const url = new URL(req.url)
        const q = (url.searchParams.get("q") || "").trim()
        const selectedMissing = parseMissingKeys(url.searchParams.get("missing"))

        const andWhere: any[] = [getScopeWhere(session)]

        if (q) {
            andWhere.push({
                OR: [
                    { title: { contains: q, mode: "insensitive" } },
                    { slug: { contains: q, mode: "insensitive" } },
                    { authorName: { contains: q, mode: "insensitive" } },
                    { series: { name: { contains: q, mode: "insensitive" } } },
                ],
            })
        }

        if (selectedMissing.length > 0) {
            andWhere.push({
                OR: selectedMissing.map((key) => buildMissingWhereForKey(key)),
            })
        }

        const novels = await (prisma as any).novel.findMany({
            where: { AND: andWhere },
            orderBy: [{ updatedAt: "desc" }],
            take: 600,
            select: {
                id: true,
                title: true,
                slug: true,
                authorName: true,
                coverUrl: true,
                description: true,
                totalChapters: true,
                updatedAt: true,
                series: {
                    select: {
                        id: true,
                        name: true,
                        slug: true,
                    },
                },
                genres: {
                    select: {
                        genre: {
                            select: {
                                id: true,
                                name: true,
                            },
                        },
                    },
                },
            },
        })

        const items = novels
            .map((novel: any) => {
                const missing = computeMissingStatus(novel)

                return {
                    id: novel.id,
                    title: novel.title,
                    slug: novel.slug,
                    authorName: novel.authorName,
                    coverUrl: novel.coverUrl,
                    description: novel.description,
                    totalChapters: novel.totalChapters,
                    updatedAt: novel.updatedAt,
                    series: novel.series,
                    genres: novel.genres.map((item: any) => item.genre),
                    missing,
                }
            })
            .filter((item: any) => hasSelectedMissing(item.missing, selectedMissing))

        return NextResponse.json({
            items,
            total: items.length,
        })
    } catch (error) {
        console.error("Failed to fetch novels with missing fields", error)
        return NextResponse.json({ error: "Failed to fetch missing-field novels" }, { status: 500 })
    }
}

export async function PATCH(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const body = await req.json()
        const updates = Array.isArray(body?.updates) ? body.updates : []

        if (updates.length === 0) {
            return NextResponse.json({ error: "Thiếu danh sách cập nhật" }, { status: 400 })
        }

        if (updates.length > 200) {
            return NextResponse.json({ error: "Chỉ hỗ trợ tối đa 200 bản ghi mỗi lần" }, { status: 400 })
        }

        const ids = updates
            .map((item: any) => (typeof item?.id === "string" ? item.id : ""))
            .filter(Boolean)

        if (ids.length === 0) {
            return NextResponse.json({ error: "Danh sách ID không hợp lệ" }, { status: 400 })
        }

        const allowedRows = await (prisma as any).novel.findMany({
            where: {
                AND: [
                    getScopeWhere(session),
                    { id: { in: ids } },
                ],
            },
            select: { id: true },
        })

        const allowedSet = new Set(allowedRows.map((row: any) => row.id))

        let updatedCount = 0
        let skippedCount = 0
        const failures: Array<{ id: string; error: string }> = []

        for (const raw of updates) {
            const id = typeof raw?.id === "string" ? raw.id : ""
            if (!id) {
                skippedCount += 1
                continue
            }

            if (!allowedSet.has(id)) {
                failures.push({ id, error: "Không có quyền cập nhật truyện này" })
                continue
            }

            const data: Record<string, any> = {}

            if (typeof raw.authorName === "string") {
                data.authorName = raw.authorName.trim()
            }

            if (typeof raw.coverUrl === "string") {
                const normalizedCover = raw.coverUrl.trim()
                data.coverUrl = normalizedCover.length > 0 ? normalizedCover : null
            } else if (raw.coverUrl === null) {
                data.coverUrl = null
            }

            if (typeof raw.description === "string") {
                data.description = raw.description.trim()
            }

            const hasGenreUpdate = Array.isArray(raw.genreIds)
            const genreIds: string[] = hasGenreUpdate
                ? Array.from(new Set((raw.genreIds as unknown[]).filter((item): item is string => typeof item === "string" && item.trim().length > 0)))
                : []

            if (Object.keys(data).length === 0 && !hasGenreUpdate) {
                skippedCount += 1
                continue
            }

            try {
                await prisma.$transaction(async (tx) => {
                    if (Object.keys(data).length > 0) {
                        await (tx as any).novel.update({
                            where: { id },
                            data,
                        })
                    }

                    if (hasGenreUpdate) {
                        await (tx as any).novelGenre.deleteMany({ where: { novelId: id } })

                        if (genreIds.length > 0) {
                            await (tx as any).novelGenre.createMany({
                                data: genreIds.map((genreId) => ({ novelId: id, genreId })),
                                skipDuplicates: true,
                            })
                        }
                    }
                })

                updatedCount += 1
            } catch (error: any) {
                failures.push({ id, error: error?.message || "Cập nhật thất bại" })
            }
        }

        return NextResponse.json({
            updatedCount,
            skippedCount,
            failureCount: failures.length,
            failures,
        })
    } catch (error) {
        console.error("Failed to patch missing-field novels", error)
        return NextResponse.json({ error: "Failed to update novels" }, { status: 500 })
    }
}
