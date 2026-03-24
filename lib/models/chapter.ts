import mongoose, { Schema, Document } from "mongoose"

export interface IChapter extends Document {
    novelId: string // Trỏ tới ID trong PostgreSQL
    number: number
    volumeNumber?: number
    volumeTitle?: string
    volumeChapterNumber?: number
    title: string
    content: string
    views: number
    createdAt: Date
}

const ChapterSchema: Schema = new Schema({
    novelId: { type: String, required: true, index: true },
    number: { type: Number, required: true },
    volumeNumber: { type: Number, default: null },
    volumeTitle: { type: String, default: null },
    volumeChapterNumber: { type: Number, default: null },
    title: { type: String, required: true },
    content: { type: String, required: true },
    views: { type: Number, default: 0 },
    createdAt: { type: Date, default: Date.now },
})

ChapterSchema.index({ novelId: 1, number: 1 }, { unique: true })
ChapterSchema.index({ createdAt: -1, novelId: 1 })

export const Chapter = mongoose.models.Chapter || mongoose.model<IChapter>("Chapter", ChapterSchema)
