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
import { submitDownload } from "@/lib/api";
import type { Job } from "@/lib/types";

interface DownloadFormProps {
  onJobCreated?: (job: Job) => void;
}

export function DownloadForm({ onJobCreated }: DownloadFormProps) {
  const [url, setUrl] = useState("");
  const [artist, setArtist] = useState("");
  const [album, setAlbum] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = url.trim().length > 0 && !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;

    setSubmitting(true);
    setError(null);

    try {
      const job = await submitDownload({
        url: url.trim(),
        artist: artist.trim() || null,
        album: album.trim() || null,
      });

      // Clear form on success
      setUrl("");
      setArtist("");
      setAlbum("");

      onJobCreated?.(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "An unexpected error occurred");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Download Audio</CardTitle>
        <CardDescription>
          Paste a YouTube or SoundCloud URL to download audio
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="url">URL *</Label>
            <Input
              id="url"
              type="url"
              placeholder="https://youtube.com/watch?v=... or https://soundcloud.com/..."
              value={url}
              onChange={(e) => setUrl(e.target.value)}
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
                value={artist}
                onChange={(e) => setArtist(e.target.value)}
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="album">Album</Label>
              <Input
                id="album"
                type="text"
                placeholder="Optional"
                value={album}
                onChange={(e) => setAlbum(e.target.value)}
              />
            </div>
          </div>

          {error && (
            <p className="text-sm text-destructive">{error}</p>
          )}

          <Button type="submit" disabled={!canSubmit} size="lg">
            {submitting ? "Submitting..." : "Download"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
