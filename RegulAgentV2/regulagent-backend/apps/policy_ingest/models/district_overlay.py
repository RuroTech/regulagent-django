from django.db import models


class DistrictOverlay(models.Model):
    jurisdiction = models.CharField(max_length=8)   # 'TX' or 'NM'
    district_code = models.CharField(max_length=16)  # '07C', '08A', '1', etc.
    source_file = models.TextField()
    requirements = models.JSONField(default=dict)
    preferences = models.JSONField(default=dict)
    plugging_chart = models.JSONField(default=dict)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('jurisdiction', 'district_code')]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.jurisdiction}/{self.district_code}"
