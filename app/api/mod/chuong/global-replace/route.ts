import { NextResponse } from "next/server"
import { getServerSession } from "next-auth/next"
import { authOptions } from "@/lib/auth"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"
import { prisma } from "@/lib/prisma"

export async function POST(req: Request) {
    const session = await getServerSession(authOptions)
    if (!session || (session.user.role !== "MOD" && session.user.role !== "ADMIN")) {
        return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    try {
        const body = await req.json()
        const { novelId, action = "replace", findText, replaceText, matchCase = false, trashWords = "", preview = false } = body

        if (!novelId) {
            return NextResponse.json({ error: "novelId is required" }, { status: 400 })
        }

        // Verify that the novel belongs to the uploader
        let novelQuery: any = { id: novelId }
        if (session.user.role !== "ADMIN") {
            novelQuery.uploaderId = session.user.id
        }

        const novel = await prisma.novel.findFirst({
            where: novelQuery,
        })

        if (!novel) {
            return NextResponse.json({ error: "Truyện không tồn tại hoặc không đủ quyền" }, { status: 403 })
        }

        await connectToMongoDB()

        let patterns: { regex: RegExp, replaceWith: string }[] = []

        if (action === "replace") {
            if (!findText) return NextResponse.json({ error: "findText is required for replace action" }, { status: 400 })
            const flags = matchCase ? "g" : "gi"
            const safeFindText = findText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
            patterns.push({ regex: new RegExp(safeFindText, flags), replaceWith: replaceText || "" })
        } else if (action === "trash") {
            let words: string[] = []
            if (Array.isArray(trashWords)) {
                words = trashWords
            } else if (typeof trashWords === "string") {
                words = trashWords.split(',').map((w: string) => w.trim()).filter((w: string) => w.length > 0)
            }

            if (words.length === 0) return NextResponse.json({ error: "No valid words provided" }, { status: 400 })
            
            words.forEach((word: string) => {
                const safeWord = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                patterns.push({ regex: new RegExp(safeWord, 'gi'), replaceWith: "" })
            })
        } else {
            return NextResponse.json({ error: "Invalid action" }, { status: 400 })
        }

        // Find all chapters for the novel
        const chapters = await Chapter.find({ novelId }).sort({ number: 1 })
        let updatedCount = 0
        let previewResults: any[] = []

        for (const chap of chapters) {
            let originalContent = chap.content || ""
            let newContent = originalContent
            let modified = false

            patterns.forEach(({ regex, replaceWith }) => {
                if (regex.test(newContent)) {
                    modified = true
                    newContent = newContent.replace(regex, replaceWith)
                }
            })

            if (modified) {
                if (preview && previewResults.length < 5) { // Limit previews to 5 chapters to save payload size
                    // Capture a small text snippet from the first pattern match
                    let snippet = ""
                    if (patterns.length > 0) {
                        const match = patterns[0].regex.exec(originalContent)
                        if (match) {
                            const matchIndex = match.index
                            const start = Math.max(0, matchIndex - 30)
                            const end = Math.min(originalContent.length, matchIndex + match[0].length + 30)
                            snippet = "..." + originalContent.substring(start, end).replace(/\n/g, ' ') + "..."
                        }
                    }

                    previewResults.push({
                        chapterId: chap._id,
                        number: chap.number,
                        title: chap.title,
                        snippet
                    })
                }

                if (!preview) {
                    chap.content = newContent
                    await chap.save()
                }
                
                updatedCount++
            }
        }

        return NextResponse.json({ 
            message: preview ? "Preview generated" : "Success",
            updatedChapters: updatedCount,
            previews: previewResults
        }, { status: 200 })

    } catch (error) {
        console.error("Global Replace Error:", error)
        return NextResponse.json({ error: "Failed to perform global replacement" }, { status: 500 })
    }
}
