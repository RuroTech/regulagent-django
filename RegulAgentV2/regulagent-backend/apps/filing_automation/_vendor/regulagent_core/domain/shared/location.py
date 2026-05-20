"""
Universal location models that work across all industries.

Whether it's an oil well, construction site, or environmental cleanup location,
these models provide consistent geographic representation.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional
from .common_types import DistanceUnit


class Coordinates(BaseModel):
    """Geographic coordinates - universal across all industries."""
    
    latitude: float = Field(..., ge=-90, le=90, description="Latitude in decimal degrees")
    longitude: float = Field(..., ge=-180, le=180, description="Longitude in decimal degrees")
    elevation_ft: Optional[float] = Field(None, description="Elevation above sea level in feet")
    datum: str = Field(default="WGS84", description="Coordinate reference system")
    accuracy_ft: Optional[float] = Field(None, description="Accuracy of coordinates in feet")
    
    @validator('latitude')
    def validate_latitude(cls, v):
        if not -90 <= v <= 90:
            raise ValueError('Latitude must be between -90 and 90 degrees')
        return v
    
    @validator('longitude')
    def validate_longitude(cls, v):
        if not -180 <= v <= 180:
            raise ValueError('Longitude must be between -180 and 180 degrees')
        return v


class Address(BaseModel):
    """Physical address - universal across all industries."""
    
    street_address: Optional[str] = Field(None, description="Street address")
    city: Optional[str] = Field(None, description="City name")
    county: Optional[str] = Field(None, description="County name")
    state_province: Optional[str] = Field(None, description="State or province")
    postal_code: Optional[str] = Field(None, description="ZIP/postal code")
    country: str = Field(default="US", description="Country code")
    
    @property
    def formatted_address(self) -> str:
        """Return formatted address string."""
        parts = []
        if self.street_address:
            parts.append(self.street_address)
        if self.city:
            parts.append(self.city)
        if self.state_province:
            parts.append(self.state_province)
        if self.postal_code:
            parts.append(self.postal_code)
        return ", ".join(parts)


class SurveyLocation(BaseModel):
    """Survey-based location description - common in regulatory filings."""
    
    section: Optional[str] = Field(None, description="Section number")
    township: Optional[str] = Field(None, description="Township designation")
    range: Optional[str] = Field(None, description="Range designation")
    block: Optional[str] = Field(None, description="Block designation")
    survey: Optional[str] = Field(None, description="Survey name")
    abstract: Optional[str] = Field(None, description="Abstract number")
    
    @property
    def formatted_survey(self) -> str:
        """Return formatted survey location."""
        parts = []
        if self.section:
            parts.append(f"Section {self.section}")
        if self.block:
            parts.append(f"Block {self.block}")
        if self.township:
            parts.append(f"Township {self.township}")
        if self.range:
            parts.append(f"Range {self.range}")
        if self.survey:
            parts.append(f"{self.survey} Survey")
        return ", ".join(parts)


class DistanceFromTown(BaseModel):
    """Distance and direction from nearest town - common in regulatory descriptions."""
    
    distance: float = Field(..., gt=0, description="Distance from town")
    distance_unit: DistanceUnit = Field(default="mi", description="Unit of distance measurement")
    direction: str = Field(..., description="Direction from town (N, NE, E, SE, S, SW, W, NW)")
    town_name: str = Field(..., description="Name of reference town")
    
    @validator('direction')
    def validate_direction(cls, v):
        valid_directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW', 
                          'North', 'Northeast', 'East', 'Southeast', 
                          'South', 'Southwest', 'West', 'Northwest']
        if v not in valid_directions:
            raise ValueError(f'Direction must be one of: {valid_directions}')
        return v
    
    @property
    def formatted_distance(self) -> str:
        """Return formatted distance description."""
        return f"{self.distance} {self.distance_unit} {self.direction} of {self.town_name}"


class Location(BaseModel):
    """Universal location model that works across all industries."""
    
    # Geographic coordinates (most precise)
    coordinates: Optional[Coordinates] = Field(None, description="GPS coordinates")
    
    # Physical address
    address: Optional[Address] = Field(None, description="Physical address")
    
    # Survey-based location (common in regulatory filings)
    survey_location: Optional[SurveyLocation] = Field(None, description="Survey-based location")
    
    # Distance from town (common in rural regulatory descriptions)
    distance_from_town: Optional[DistanceFromTown] = Field(None, description="Distance from nearest town")
    
    # Additional location identifiers
    parcel_id: Optional[str] = Field(None, description="Tax parcel identifier")
    legal_description: Optional[str] = Field(None, description="Legal property description")
    
    # Location quality indicators
    location_source: Optional[str] = Field(None, description="Source of location data")
    location_accuracy: Optional[str] = Field(None, description="Accuracy of location data")
    
    @property
    def best_description(self) -> str:
        """Return the best available location description."""
        if self.distance_from_town:
            return self.distance_from_town.formatted_distance
        elif self.address:
            return self.address.formatted_address
        elif self.survey_location:
            return self.survey_location.formatted_survey
        elif self.coordinates:
            return f"Lat: {self.coordinates.latitude}, Lon: {self.coordinates.longitude}"
        else:
            return "Location not specified"
    
    @property
    def has_precise_coordinates(self) -> bool:
        """Check if location has precise GPS coordinates."""
        return self.coordinates is not None
    
    @property
    def has_address(self) -> bool:
        """Check if location has a physical address."""
        return self.address is not None and bool(self.address.formatted_address.strip(", "))
    
    def validate_completeness(self) -> list[str]:
        """Validate location completeness and return any issues."""
        issues = []
        
        if not any([self.coordinates, self.address, self.survey_location, self.distance_from_town]):
            issues.append("No location information provided")
        
        if self.coordinates and self.coordinates.accuracy_ft and self.coordinates.accuracy_ft > 100:
            issues.append(f"Coordinate accuracy is low: {self.coordinates.accuracy_ft} ft")
        
        return issues
