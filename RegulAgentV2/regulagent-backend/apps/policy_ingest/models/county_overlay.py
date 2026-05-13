from django.db import models

from .district_overlay import DistrictOverlay


class CountyOverlay(models.Model):
    district_overlay = models.ForeignKey(
        DistrictOverlay,
        on_delete=models.CASCADE,
        related_name='counties',
    )
    county_name = models.CharField(max_length=128)
    requirements = models.JSONField(default=dict)
    preferences = models.JSONField(default=dict)
    notes = models.JSONField(default=list)
    county_procedures = models.JSONField(default=dict)
    formation_data = models.JSONField(default=dict)

    class Meta:
        unique_together = [('district_overlay', 'county_name')]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.district_overlay} / {self.county_name}"
