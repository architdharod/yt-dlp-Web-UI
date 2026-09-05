import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { probeUrl, submitDownload } from "@/lib/api";
import { MAX_FOLDER_NAME } from "@/lib/preview";
import type { CollectionPreview, Job } from "@/lib/types";

/** What the form is waiting on, which is what its button says. */
type Phase = "idle" | "probing" | "submitting";

const BUTTON_LABEL: Record<Phase, string> = {
  idle: "Download",
  probing: "Checking URL...",
  submitting: "Submitting...",
};

/**
 * What the user typed.
 *
 * Held by the tab rather than the form: a collection URL replaces the form
 * with the checklist, and Cancel has to come back to the fields as they were.
 */
export interface DownloadFields {
  url: string;
  artist: string;
  album: string;
}

/** An untouched form, and what a queued download resets it to. */
export const BLANK_FIELDS: DownloadFields = { url: "", artist: "", album: "" };

interface DownloadFormProps {
  fields: DownloadFields;
  onFieldsChange: (fields: DownloadFields) => void;
  onJobCreated?: (job: Job) => void;
  /**
   * A collection URL: hand the checklist over instead of queueing anything.
   * The trimmed artist goes with it, because the preview the backend answers
   * with echoes its own suggestion whatever it deduped against.
   */
  onCollection?: (preview: CollectionPreview, artist: string) => void;
}

export function DownloadForm({
  fields,
  onFieldsChange,
  onJobCreated,
  onCollection,
}: DownloadFormProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  const { url, artist, album } = fields;
  const canSubmit = url.trim().length > 0 && phase === "idle";

  function setField(field: keyof DownloadFields, value: string) {
    onFieldsChange({ ...fields, [field]: value });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;

    setPhase("probing");
    setError(null);

    try {
      // Only the backend can tell a track from a collection, so every submit
      // asks first. The artist goes with the question: it is the folder the
      // preview's "in library" marks have to be about.
      const probe = await probeUrl(url.trim(), artist.trim() || null);

      if (probe.type === "collection") {
        // The fields stay filled — they live in the tab — so Cancel on the
        // preview comes back to them untouched.
        onCollection?.(probe.preview, artist.trim());
        return;
      }

      setPhase("submitting");
      const job = await submitDownload({
        url: url.trim(),
        artist: artist.trim() || null,
        album: album.trim() || null,
      });

      // Clear form on success
      onFieldsChange(BLANK_FIELDS);

      onJobCreated?.(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "An unexpected error occurred");
    } finally {
      setPhase("idle");
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Download Audio</CardTitle>
        <CardDescription>
          Paste a YouTube, SoundCloud, or Bandcamp URL. A single track is
          queued straight away; a playlist, album, or artist page opens a
          checklist to pick from.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="url">URL *</Label>
            <Input
              id="url"
              type="url"
              placeholder="https://youtube.com/watch?v=... or a playlist, album, or artist page"
              value={url}
              onChange={(e) => setField("url", e.target.value)}
              required
            />
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <Label htmlFor="artist">Artist</Label>
              <Input
                id="artist"
                type="text"
                placeholder="Optional"
                maxLength={MAX_FOLDER_NAME}
                value={artist}
                onChange={(e) => setField("artist", e.target.value)}
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="album">Album</Label>
              <Input
                id="album"
                type="text"
                placeholder="Optional"
                maxLength={MAX_FOLDER_NAME}
                value={album}
                aria-describedby="album-help"
                onChange={(e) => setField("album", e.target.value)}
              />
              <p id="album-help" className="text-xs text-muted-foreground">
                Ignored for playlists and albums: each track keeps the album the
                source gave it.
              </p>
            </div>
          </div>

          {error && (
            <p className="text-sm text-destructive">{error}</p>
          )}

          <Button type="submit" disabled={!canSubmit} size="lg">
            {BUTTON_LABEL[phase]}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
