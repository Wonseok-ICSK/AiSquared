/** 404 page for unknown SPA routes. */

import { Link } from "react-router-dom";
import { MapPinOff } from "lucide-react";
import { EmptyState } from "../components/ui";
import { useTitle } from "../lib/hooks";
import { tr } from "../lib/i18n";

/** Shown for unknown paths. */
export function NotFound() {
  useTitle(tr("Not found", "페이지 없음"));
  return (
    <EmptyState
      icon={<MapPinOff size={26} />}
      title={tr("Page not found", "페이지를 찾을 수 없어요")}
      action={
        <Link className="btn btn--primary" to="/">
          {tr("Back to trips", "내 여행으로 돌아가기")}
        </Link>
      }
    >
      {tr(
        "This page does not exist. It may have been moved or deleted.",
        "존재하지 않는 페이지예요. 이동되었거나 삭제되었을 수 있어요.",
      )}
    </EmptyState>
  );
}
