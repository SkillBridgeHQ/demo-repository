"use client";

import { use } from "react";
import VerifyPanel from "./verify-panel";

type VerifyRouteContentProps = {
  searchParams: Promise<{
    token?: string | string[];
  }>;
};

export default function VerifyRouteContent({
  searchParams,
}: VerifyRouteContentProps) {
  const params = use(searchParams);

  const token =
    typeof params.token === "string" ? params.token : "";

  return <VerifyPanel initialToken={token} />;
}
