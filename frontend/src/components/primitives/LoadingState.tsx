import { useEffect, useState } from "react";

/** Chevron wavefront: each cell lights 90ms after its neighbour, 650ms cycle. */
const CHEVRON = Array.from({ length: 9 }, (_, i) => (i % 3) + Math.abs(Math.floor(i / 3) - 1));

function useElapsed(running: boolean) {
  const [tenths, setTenths] = useState(0);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setTenths((d) => d + 1), 100);
    return () => window.clearInterval(timer);
  }, [running]);
  const total = tenths / 10;
  return total < 60 ? `${total.toFixed(1)}s` : `${Math.floor(total / 60)}m ${(total % 60).toFixed(1)}s`;
}

/** Pixel-grid loader with a shimmering label and a live elapsed timer. */
export function LoadingState({ label, running = true }: { label: string; running?: boolean }) {
  const elapsed = useElapsed(running);
  return (
    <div className="loading-state" role="status">
      <span className="loading-grid" aria-hidden="true">
        {CHEVRON.map((delay, index) => (
          <span key={index} style={{ animation: `pixel-on 650ms ease-in-out ${delay * 90}ms infinite` }} />
        ))}
      </span>
      <span className="loading-label">{label}</span>
      <span className="loading-time tabular">{elapsed}</span>
    </div>
  );
}
