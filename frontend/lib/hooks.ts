"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  /** 404 from the backend: the artifact has not been produced yet. */
  missing: boolean;
  loading: boolean;
  reload: () => void;
}

/** Run an async loader, re-running when `deps` change or `reload()` is called.
 *  A 404 is surfaced as `missing` rather than `error`, because "not generated
 *  yet" and "the backend failed" must not look the same in the UI. */
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  options: { skip?: boolean } = {},
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [loading, setLoading] = useState(!options.skip);
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (options.skip) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setMissing(false);
    loaderRef
      .current()
      .then((value) => {
        if (cancelled) return;
        setData(value);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setData(null);
        if (err instanceof ApiError && err.isMissing) {
          setMissing(true);
          setError(err.message);
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, options.skip]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, missing, loading, reload };
}

/** A mutation with its own pending/error state, so a button can report what
 *  actually happened instead of flashing a fake success. */
export function useAction<Args extends unknown[], R>(
  fn: (...args: Args) => Promise<R>,
): {
  run: (...args: Args) => Promise<R | undefined>;
  pending: boolean;
  error: string | null;
  result: R | null;
  reset: () => void;
} {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<R | null>(null);

  // The action is kept in a ref and refreshed every render. `run` itself is
  // stable, so passing it to a button does not re-render the tree, but the
  // function it calls is always the current one -- a `useCallback(fn, [])`
  // here would freeze the first closure and keep submitting the form state
  // as it was on mount, which is how an edited DAX expression silently
  // became a no-op save of the original.
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const run = useCallback(async (...args: Args) => {
    setPending(true);
    setError(null);
    try {
      const value = await fnRef.current(...args);
      setResult(value);
      return value;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return undefined;
    } finally {
      setPending(false);
    }
  }, []);

  const reset = useCallback(() => {
    setError(null);
    setResult(null);
  }, []);

  return { run, pending, error, result, reset };
}
