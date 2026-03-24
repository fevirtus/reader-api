import mongoose, { Schema, Document } from "mongoose"

export interface IEditorRecommendation extends Document {
  novelId: string
  editorId: string
  createdAt: Date
  updatedAt: Date
}

const EditorRecommendationSchema: Schema = new Schema(
  {
    novelId: { type: String, required: true, index: true },
    editorId: { type: String, required: true, index: true },
  },
  {
    timestamps: true,
  }
)

EditorRecommendationSchema.index({ novelId: 1, editorId: 1 }, { unique: true })
EditorRecommendationSchema.index({ createdAt: -1 })

export const EditorRecommendation =
  mongoose.models.EditorRecommendation ||
  mongoose.model<IEditorRecommendation>("EditorRecommendation", EditorRecommendationSchema)
