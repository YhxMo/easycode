import { useCallback, useState } from "react";

/** A `{key: boolean}` map persisted under one localStorage key. */
export function usePersistedFlags(storageKey: string) {
  const [flags, setFlags] = useState<Record<string, boolean>>(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      return saved ? JSON.parse(saved) : {};
    } catch {
      return {};
    }
  });

  const toggle = useCallback(
    (key: string) => {
      setFlags((prev) => {
        const next = { ...prev, [key]: !prev[key] };
        try {
          localStorage.setItem(storageKey, JSON.stringify(next));
        } catch {
          // localStorage 不可用（隐私模式等）——仅内存态，忽略即可
        }
        return next;
      });
    },
    [storageKey],
  );

  return [flags, toggle] as const;
}
