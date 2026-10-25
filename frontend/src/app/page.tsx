import { AccessGate } from "@/components/access-gate";
import { RecommendationWorkspace } from "@/components/recommendation-workspace";

export default function Home() {
  return (
    <AccessGate>
      <RecommendationWorkspace />
    </AccessGate>
  );
}
