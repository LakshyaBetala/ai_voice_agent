"use client";
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";

export function CsvUploadDialog() {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [isPending, start] = useTransition();
  const router = useRouter();

  async function upload() {
    if (!file) return;
    const text = await file.text();
    start(async () => {
      const res = await fetch("/api/leads/import", { method: "POST", body: text });
      const body = await res.json();
      if (!res.ok) {
        toast.error(body.error ?? "upload failed");
        return;
      }
      toast.success(
        `Inserted ${body.inserted}. Invalid: ${body.invalid.length}. Duplicates: ${body.duplicatesInFile.length}.`,
      );
      setOpen(false);
      setFile(null);
      router.refresh();
    });
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline">Upload CSV</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Upload leads CSV</DialogTitle>
        </DialogHeader>
        <p className="mb-3 text-sm text-muted-foreground">
          Columns: <code>name,phone,company,industry,source,notes</code>.
          Max 10,000 rows.
        </p>
        <Input
          type="file"
          accept=".csv"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <div className="mt-4 flex justify-end">
          <Button disabled={!file || isPending} onClick={upload}>
            {isPending ? "Uploading…" : "Upload"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
