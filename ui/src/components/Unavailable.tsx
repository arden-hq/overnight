import { Button } from "@/components/ui/button";
import { ApiFailure } from "../lib/api";

export function Unavailable({ what, error, onRetry }: { what: string; error: unknown; onRetry?: () => void }) {
  const failure = error instanceof ApiFailure ? error : undefined;
  const unreachable = !failure || failure.code === "unreachable" || failure.status === 0 || failure.status >= 502;
  return (
    <div className="empty pad unavailable">
      <p>{unreachable ? "Overnight could not reach its local service." : failure.status === 404 ? `No ${what}.` : `Overnight could not load ${what}.`}</p>
      <p className="mono">
        {failure ? `${failure.status || "network"} ${failure.code}: ${failure.message}` : error instanceof Error ? error.message : String(error)}
      </p>
      {unreachable && <p className="mono">.venv/bin/overnight serve</p>}
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}
