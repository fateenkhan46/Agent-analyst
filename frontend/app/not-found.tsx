import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-lg py-16 text-center">
      <h1 className="text-lg font-semibold text-ink">Page not found</h1>
      <p className="mt-2 text-sm text-muted">
        That URL does not match any page in this application.
      </p>
      <Link href="/" className="mt-4 inline-block text-sm text-brand hover:underline">
        Back to the dashboard
      </Link>
    </div>
  );
}
