import { NextResponse } from "next/server"
import connectToMongoDB from "@/lib/mongoose"
import { Chapter } from "@/lib/models/chapter"

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ chapterId: string }> }
) {
  try {
    const { chapterId } = await params

    await connectToMongoDB()

    const chapter = await Chapter.findById(chapterId).lean()

    if (!chapter) {
      return NextResponse.json({ error: "Chapter not found" }, { status: 404 })
    }

    // Fetch prev/next chapter numbers for navigation
    const [prevChapter, nextChapter] = await Promise.all([
      Chapter.findOne({ novelId: (chapter as any).novelId, number: (chapter as any).number - 1 })
        .select("_id number")
        .lean(),
      Chapter.findOne({ novelId: (chapter as any).novelId, number: (chapter as any).number + 1 })
        .select("_id number")
        .lean(),
    ])

    return NextResponse.json({
      id: (chapter as any)._id.toString(),
      novelId: (chapter as any).novelId,
      number: (chapter as any).number,
      title: (chapter as any).title,
      content: (chapter as any).content,
      views: (chapter as any).views,
      volumeNumber: (chapter as any).volumeNumber ?? null,
      volumeTitle: (chapter as any).volumeTitle ?? null,
      volumeChapterNumber: (chapter as any).volumeChapterNumber ?? null,
      createdAt: ((chapter as any).createdAt as Date).toISOString(),
      prevChapterId: prevChapter ? (prevChapter as any)._id.toString() : null,
      prevChapterNumber: prevChapter ? (prevChapter as any).number : null,
      nextChapterId: nextChapter ? (nextChapter as any)._id.toString() : null,
      nextChapterNumber: nextChapter ? (nextChapter as any).number : null,
    })
  } catch (error) {
    console.error("Chapter content error:", error)
    return NextResponse.json({ error: "Internal Server Error" }, { status: 500 })
  }
}
