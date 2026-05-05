import time
import mlflow
from Levenshtein import distance as lev_distance


class Evaluator:
    def __init__(self):
        mlflow.set_tracking_uri("sqlite:///mlruns.db")
        mlflow.set_experiment("agentic_pipeline_eval")
        self.runs = {}

    def start_inference(self, doc_id: str):
        self.runs[doc_id] = {"start_time": time.time()}

    def end_inference(self, doc_id: str):
        if doc_id in self.runs:
            self.runs[doc_id]["inference_time"] = time.time() - self.runs[doc_id]["start_time"]

    def log_metrics(self, doc_id: str, original_md: str, modified_md: str,
                    user_rating: int, download: bool, time_consumed: float):
        edit_dist = lev_distance(original_md, modified_md)
        inference_time = self.runs.get(doc_id, {}).get("inference_time", 0.0)

        with mlflow.start_run(run_name=f"eval_{doc_id}"):
            mlflow.log_metric("inference_time", inference_time)
            mlflow.log_metric("user_rating", user_rating)
            mlflow.log_metric("edit_distance", edit_dist)
            mlflow.log_metric("time_consumed_to_modify", time_consumed)
            mlflow.log_param("downloaded", download)
            # Note: Real BLEU/COMET requires reference translations.
            # These are placeholder estimates for the demo.
            mlflow.log_metric("COMET_estimate", max(0, 100 - (edit_dist * 0.1)))
            mlflow.log_metric("BLEU_estimate", max(0, 100 - (edit_dist * 0.5)))

        print(f"[Eval] Metrics logged for {doc_id}. Edit dist: {edit_dist}")
        return {"edit_distance": edit_dist, "inference_time": inference_time}
