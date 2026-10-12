/** 404 page for unknown SPA routes: the sunrise mark, one serif line and a way home. */

import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { BrandMark } from "../components/BrandMark";
import { useTitle } from "../lib/hooks";
import { tr } from "../lib/i18n";

/** Shown for unknown paths. */
export function NotFound() {
  useTitle(tr("Not found", "페이지 없음"));
  return (
    <div className="not-found">
      <BrandMark size={52} className="not-found__mark" />
      <h1 className="not-found__title">{tr("There's nothing here.", "여기에는 아무것도 없어요.")}</h1>
      <p className="page-lede">
        {tr(
          "This page does not exist. It may have been moved or deleted.",
          "존재하지 않는 페이지예요. 이동되었거나 삭제되었을 수 있어요.",
        )}
      </p>
      <Link className="btn btn--primary" to="/">
        <ArrowLeft size={16} aria-hidden />
        {tr("Back to trips", "내 여행으로 돌아가기")}
      </Link>
    </div>
  );
}
