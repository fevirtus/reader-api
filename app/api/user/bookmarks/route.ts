import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import { prisma } from "@/lib/prisma"

function toUTCDateOnly(value: Date): Date {
    return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), value.getUTCDate()))
}

async function upsertDailyNovelView(novelId: string, day: Date) {
    const delegate = (prisma as any).novelViewDaily
    if (!delegate || typeof delegate.upsert !== "function") return

    await delegate.upsert({
        where: {
            novelId_day: {
                novelId,
                day,
            },
        },
        update: {
            views: { increment: 1 },
        },
        create: {
            novelId,
            day,
            views: 1,
        },
    })
}

// Lấy danh sách bookmark
export async function GET(req: Request) {
    try {
        const session = await getServerSession(authOptions)
        if (!session?.user?.id) {
            return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
        }

        const bookmarks = await prisma.bookmark.findMany({
            where: { userId: session.user.id },
            include: { novel: true },
            orderBy: { createdAt: "desc" }
        })

        return NextResponse.json(bookmarks)
    } catch (error) {
        console.error("GET Bookmarks Error", error)
        return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
    }
}

// Thêm, cập nhật hoặc xóa bookmark
export async function POST(req: Request) {
    try {
        const session = await getServerSession(authOptions)
        if (!session?.user?.id) {
            return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
        }

        const body = await req.json()
        const { action, novelId, lastChapterId, lastChapterNumber } = body

        if (!novelId || !action) {
            return NextResponse.json({ error: "Bad Request" }, { status: 400 })
        }

        if (action === "toggle") {
            const existing = await prisma.bookmark.findUnique({
                where: {
                    userId_novelId: {
                        userId: session.user.id,
                        novelId,
                    }
                }
            })

            if (existing) {
                // Xoá
                await prisma.$transaction([
                    prisma.bookmark.delete({ where: { id: existing.id } }),
                    prisma.novel.update({ where: { id: novelId }, data: { bookmarkCount: { decrement: 1 } } })
                ])
                return NextResponse.json({ status: "removed" })
            } else {
                // Thêm mới
                const newBookmark = await prisma.$transaction(async (tx) => {
                    const b = await tx.bookmark.create({
                        data: {
                            userId: session.user.id,
                            novelId,
                            lastChapterId,
                            lastChapterNumber
                        }
                    })
                    await tx.novel.update({ where: { id: novelId }, data: { bookmarkCount: { increment: 1 } } })
                    return b
                })
                return NextResponse.json({ status: "added", bookmark: newBookmark })
            }
        } else if (action === "updateProgress") {
            // Cập nhật tiến độ lưu trang
            if (!lastChapterId || !lastChapterNumber) {
                return NextResponse.json({ error: "Missing chapter info" }, { status: 400 })
            }

            // Lấy bookmark cũ (nếu có)
            const existingBookmark = await prisma.bookmark.findUnique({
                where: {
                    userId_novelId: {
                        userId: session.user.id,
                        novelId,
                    }
                }
            })

            let newReadChapters: number[] = []
            let newHasCountedView = false
            let shouldIncrementNovelView = false

            if (existingBookmark) {
                newReadChapters = existingBookmark.readChapters || []
                newHasCountedView = existingBookmark.hasCountedView

                // Nếu chương này chưa đọc, thêm vào mảng
                if (!newReadChapters.includes(lastChapterNumber)) {
                    newReadChapters.push(lastChapterNumber)
                }

                // Nếu đọc đủ 5 chương và chưa từng đếm view
                if (newReadChapters.length >= 5 && !newHasCountedView) {
                    newHasCountedView = true
                    shouldIncrementNovelView = true
                }
            } else {
                newReadChapters = [lastChapterNumber]
                // Chưa đủ 5 chương ngay từ lần đầu tạo
            }

            const bookmark = await prisma.bookmark.upsert({
                where: {
                    userId_novelId: {
                        userId: session.user.id,
                        novelId,
                    }
                },
                update: {
                    lastChapterId,
                    lastChapterNumber,
                    readChapters: newReadChapters,
                    hasCountedView: newHasCountedView
                },
                create: {
                    userId: session.user.id,
                    novelId,
                    lastChapterId,
                    lastChapterNumber,
                    readChapters: newReadChapters,
                    hasCountedView: newHasCountedView
                }
            })

            if (shouldIncrementNovelView) {
                const day = toUTCDateOnly(new Date())
                await prisma.novel.update({
                    where: { id: novelId },
                    data: { views: { increment: 1 } }
                })
                await upsertDailyNovelView(novelId, day)
            }

            return NextResponse.json({ status: "updated", bookmark })
        }

        return NextResponse.json({ error: "Invalid action" }, { status: 400 })

    } catch (error) {
        console.error("POST Bookmarks Error", error)
        return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
    }
}
