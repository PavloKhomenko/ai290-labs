from datetime import UTC, datetime

from airflow.sdk import Param, dag, get_current_context, task
from airflow.timetables.interval import CronDataIntervalTimetable

from ukwiki_patrol import pipeline


def batch_date() -> str:
    ctx = get_current_context()
    return ctx["params"]["date"] or ctx["data_interval_start"].strftime("%Y-%m-%d")


@dag(
    schedule=CronDataIntervalTimetable("@daily", timezone="UTC"),
    start_date=datetime(2026, 9, 20, tzinfo=UTC),
    catchup=True,
    max_active_runs=1,  # build_features counts prior edits from earlier days, so days run in order
    params={
        "date": Param(None, type=["null", "string"], description="YYYY-MM-DD; default: data interval"),
        "corrupt": Param(False, type="boolean", description="duplicate 20% of rows to test the gate"),
    },
    tags=["ukwiki-patrol"],
)
def ukwiki_daily():
    @task
    def extract() -> str:
        return pipeline.extract(batch_date())

    @task
    def validate(raw_path: str) -> str:
        pipeline.validate(raw_path)
        return raw_path

    @task
    def transform(raw_path: str) -> str:
        return pipeline.transform(raw_path, batch_date(), get_current_context()["params"]["corrupt"])

    @task
    def load(clean_path: str) -> int:
        return pipeline.load(clean_path, batch_date())

    @task
    def build_features(_rows: int) -> str:
        return pipeline.build_features(batch_date())

    @task
    def quality_check(_path: str) -> dict:
        return pipeline.quality_check(batch_date())

    quality_check(build_features(load(transform(validate(extract())))))


ukwiki_daily()
