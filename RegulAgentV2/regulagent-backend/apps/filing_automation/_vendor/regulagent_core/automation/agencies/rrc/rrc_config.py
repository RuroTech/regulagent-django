"""Texas Railroad Commission (RRC) automation configuration."""

from typing import Dict, Any, List
from ...base.data_models import SelectorConfig, TabConfig, TabType, WorkflowStep


# RRC System URLs and Endpoints
RRC_URLS = {
    "login": "https://webapps.rrc.texas.gov/security/login.do",
    "gis_viewer": "https://gis.rrc.texas.gov/GISViewer/",
    "base": "https://webapps.rrc.texas.gov"
}

# Form-specific selector configurations
RRC_SELECTORS = {
    # Authentication selectors
    "login_username": SelectorConfig(
        primary='input[name="login"]',
        fallbacks=['input[name="username"]', '#username', '#login'],
        description="Username field"
    ),
    "login_password": SelectorConfig(
        primary='input[name="password"]',
        fallbacks=['#password', 'input[type="password"]'],
        description="Password field"
    ),
    "login_submit": SelectorConfig(
        primary='input[type="submit"][value="Submit"]',
        fallbacks=['button[type="submit"]', 'input[value="Submit"]'],
        description="Login submit button"
    ),
    
    # Navigation selectors
    "nav_dropdown": SelectorConfig(
        primary='select[name="go"]',
        fallbacks=['select.nav-select', '#navigation-select'],
        description="Navigation dropdown"
    ),
    "nav_go_button": SelectorConfig(
        primary='input[value="Go"]',
        fallbacks=['button:has-text("Go")', '.go-btn'],
        description="Navigation go button"
    ),
    
    # W3A Application selectors
    "iframe_container": SelectorConfig(
        primary='#receiver',
        fallbacks=['.iframe-container', 'iframe'],
        description="Main application iframe"
    ),
    "w3a_settings_button": SelectorConfig(
        primary='.solution-list-item:has-text("W3A") .dropdown-toggle',
        fallbacks=['.w3a-settings', '[data-target*="w3a"]'],
        description="W3A settings dropdown"
    ),
    "w3a_open_link": SelectorConfig(
        primary='.solution-list-item:has-text("W3A") a[role="menuitem"]:has-text("Open")',
        fallbacks=['a:has-text("Open W3A")', '.w3a-open'],
        description="W3A open link"
    ),
    "w3a_create_button": SelectorConfig(
        primary='.back-btn:has-text("Create")',
        fallbacks=['button:has-text("Create")', '.create-btn'],
        description="W3A create button"
    ),
    
    # W3A Form Field Selectors
    "api_number_field": SelectorConfig(
        primary='input[type="text"]',  # First text input is usually API
        fallbacks=['.api-field', 'input[placeholder*="API"]'],
        description="API number input field"
    ),
    "lease_dropdown": SelectorConfig(
        primary='#field-2fe997f9-2544-40a4-9a7c-bf3c8d4bf885 .Select-control',
        fallbacks=['.lease-select .Select-control', '.Select-control'],
        description="Lease selection dropdown"
    ),
    "lease_option": SelectorConfig(
        primary='.Select-option',
        fallbacks=['.lease-option', 'option'],
        description="Lease dropdown option"
    ),
    
    # Distance and location fields
    "distance_direction_field": SelectorConfig(
        primary='#field-bb4f54e9-59ec-4de5-8c12-7b58ae5add48 input[type="text"]',
        fallbacks=['.distance-field input', 'input[placeholder*="distance"]'],
        description="Distance and direction field"
    ),
    
    # Well type selection
    "well_type_dropdown": SelectorConfig(
        primary='#field-562c2e2b-6eb6-4598-b05a-050438d94dc2 .Select-control',
        fallbacks=['.well-type .Select-control', '.type-select .Select-control'],
        description="Well type dropdown"
    ),
    "well_type_options": SelectorConfig(
        primary='.Select-option',
        fallbacks=['.option', 'option'],
        description="Well type options"
    ),
    
    # Completion type
    "completion_type_section": SelectorConfig(
        primary='#field-54e02511-5e33-4bc9-8496-32546ac5643f',
        fallbacks=['.completion-type-section', '.completion-section'],
        description="Completion type section"
    ),
    "completion_single_radio": SelectorConfig(
        primary='input[type="radio"][value="Single"]',
        fallbacks=['input[value="Single"]', 'label:has-text("Single") input'],
        description="Single completion radio button"
    ),
    
    # Previous notice question
    "previous_notice_section": SelectorConfig(
        primary='#field-be5805bc-4b4a-4518-b5bc-dac27e8acef1',
        fallbacks=['.previous-notice', '.notice-section'],
        description="Previous notice section"
    ),
    "previous_notice_no": SelectorConfig(
        primary='input[type="radio"][value="No"]',
        fallbacks=['input[value="No"]', 'label:has-text("No") input'],
        description="No previous notice radio"
    ),
    
    # GAU attachment
    "gau_section": SelectorConfig(
        primary='#field-ab03a8eb-152d-421f-8646-4fb66c805607',
        fallbacks=['.gau-section', '.attachment-section'],
        description="GAU attachment section"
    ),
    "gau_add_button": SelectorConfig(
        primary='button:has-text("Add")',
        fallbacks=['.add-btn', '.attachment-add'],
        description="GAU add button"
    ),
    "gau_file_input": SelectorConfig(
        primary='input[type="file"]',
        fallbacks=['.file-input'],
        description="GAU file input"
    ),
    
    # Area review section
    "area_review_section": SelectorConfig(
        primary='#field-8981ad5b-5aca-447d-986a-bff6b5c1640d, #field-d3cafd19-37e0-46be-bd5d-abfee70b79f7',
        fallbacks=['.area-review', '.review-section'],
        description="Area review section"
    ),
    "area_review_add_buttons": SelectorConfig(
        primary='div[id*="field-8981ad5b"] button:has-text("Add"), div[id*="field-d3cafd19"] button:has-text("Add")',
        fallbacks=['.area-add-btn', 'button:has-text("Add")'],
        description="Area review add buttons"
    ),
    "depth_inputs": SelectorConfig(
        primary='input[placeholder*="Depth" i], input[title*="depth" i], .react-grid-Cell input',
        fallbacks=['.depth-input', 'input[type="number"]'],
        description="Depth input fields"
    ),
    
    # Cementing company info
    "cementing_field": SelectorConfig(
        primary='#field-b1db5ad1-323a-4a22-9f5e-bfa60ab5d50b textarea',
        fallbacks=['.cementing-textarea', 'textarea'],
        description="Cementing company info textarea"
    ),
    
    # Contact and date fields
    "plugging_date_field": SelectorConfig(
        primary='#field-31007eb0-7ec1-4f25-ab78-9b2309d154c2 input[type="text"]',
        fallbacks=['.date-field input', 'input[type="date"]'],
        description="Anticipated plugging date field"
    ),
    "title_field": SelectorConfig(
        primary='#field-0ef0b46f-4cb1-4f28-8519-89d5d3ec0cac input[type="text"]',
        fallbacks=['.title-field', 'input[placeholder*="title"]'],
        description="Contact title field"
    ),
    "phone_field": SelectorConfig(
        primary='.phone-txt input.form-control',
        fallbacks=['.phone-field input', 'input[type="tel"]'],
        description="Phone number field"
    ),
    "email_field": SelectorConfig(
        primary='#field-edab88d9-df21-4aa9-8591-6707ee259ab4 input[type="text"]',
        fallbacks=['.email-field input', 'input[type="email"]'],
        description="Email field"
    ),
    
    # Agreement and submission
    "agreement_section": SelectorConfig(
        primary='#field-e07e7be3-25d6-4193-b2c3-a954b648eafe',
        fallbacks=['.agreement-section', '.terms-section'],
        description="Agreement section"
    ),
    "agreement_checkbox": SelectorConfig(
        primary='input[type="checkbox"]',
        fallbacks=['.agreement-checkbox', '.terms-checkbox'],
        description="I agree checkbox"
    ),
    "save_button": SelectorConfig(
        # Scoped to the top toolbar's pull-left div to avoid per-row area-review
        # Save buttons that share the same btn btn-default class.
        # DOM (w3a_form_dom.html): .workitem-action-bar > .pull-left > button.btn.btn-default
        primary='.workitem-action-bar .pull-left button.btn.btn-default',
        fallbacks=['button.btn.btn-default:has-text("Save")', 'button:has-text("Save")', '.save-btn'],
        description="Save button (top toolbar, scoped away from area-review rows)"
    ),
    "submit_button": SelectorConfig(
        primary='button.btn-primary.btn.btn-default:has-text("Submit")',
        fallbacks=['button:has-text("Submit")', '.submit-btn'],
        description="Submit button"
    ),
    
    # GIS Viewer selectors
    "gis_search_input": SelectorConfig(
        primary='input[placeholder*="Find well api" i], input[placeholder*="address" i]',
        fallbacks=['.search-input', 'input[type="search"]'],
        description="GIS search input"
    ),
    "gis_identify_tool": SelectorConfig(
        primary='button[title*="Identify" i]',
        fallbacks=['button[aria-label*="Identify" i]', '.identify-tool', 'button:has-text("i")'],
        description="GIS identify tool"
    ),
    "gis_wells_layer": SelectorConfig(
        primary='select option[value*="wells" i]',
        fallbacks=['select option:has-text("Wells")', 'option:has-text("wells")'],
        description="GIS wells layer option"
    ),
    "gis_well_markers": SelectorConfig(
        primary='.well-marker',
        fallbacks=['.esri-graphic', '[class*="well"]', 'circle[fill*="blue"]', 'circle[fill*="green"]'],
        description="GIS well markers"
    ),
    "gis_identity_results": SelectorConfig(
        primary='[class*="identity" i], [title*="identity" i], .popup-content',
        fallbacks=['.info-window', '.esri-popup'],
        description="GIS identity results popup"
    ),
    "gis_drilling_permits_link": SelectorConfig(
        primary='a:has-text("Drilling Permits")',
        fallbacks=['button:has-text("Drilling Permits")', 'span:has-text("Drilling Permits")'],
        description="Drilling permits link in GIS"
    ),
    "gis_lease_name_link": SelectorConfig(
        primary='a[style*="color" i]',  # Highlighted links
        fallbacks=['span[style*="color" i]', 'td > a', 'a:has-text("UNIVERSITY")'],
        description="GIS lease name link"
    )
}

# GIS Data extraction patterns (from your comprehensive script)
GIS_EXTRACTION_PATTERNS = {
    "distance_direction": [
        r'Distance.*?Direction.*?nearby.*?City.*?(\d+\.?\d*)\s*miles?\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW|northeast|northwest|southeast|southwest)\s*of\s*([A-Z][A-Z\s]+)',
        r'(\d+\.?\d*)\s*miles?\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW|northeast|northwest|southeast|southwest)\s*of\s*([A-Z][A-Z\s]+)',
        r'Distance.*?(\d+\.?\d*)\s*miles?\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW).*?([A-Z][A-Z\s]+)',
        r'(\d+\.?\d*)\s*(miles?|mi)\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW)\s*of\s*([A-Z][A-Z\s]+)'
    ],
    "distance_only": [
        r'Distance.*?(\d+\.?\d*)\s*miles?',
        r'(\d+\.?\d*)\s*miles?',
        r'(\d+\.?\d*)\s*mi'
    ],
    "direction_only": [
        r'Direction.*?(north|south|east|west|N|S|E|W|NE|NW|SE|SW|northeast|northwest|southeast|southwest)',
        r'(north|south|east|west|N|S|E|W|NE|NW|SE|SW|northeast|northwest|southeast|southwest)\s*of',
        r'miles?\s*(north|south|east|west|N|S|E|W|NE|NW|SE|SW|northeast|northwest|southeast|southwest)'
    ],
    "town_patterns": [
        r'Nearest.*?Town.*?[:\-\s]*([A-Z][A-Z\s]+)',
        r'Town.*?[:\-\s]*([A-Z]{3,})',
        r'City.*?[:\-\s]*([A-Z][A-Z\s]+)',
        r'(?:miles?\s*(?:north|south|east|west|N|S|E|W|NE|NW|SE|SW|northeast|northwest|southeast|southwest)\s*of\s*)([A-Z][A-Z\s]+)'
    ],
    "well_type": [
        r'(oil|gas|water|injection)\s*well',
        r'Type[:\s]*(Oil|Gas|Water|Injection)',
        r'Production[:\s]*Type[:\s]*(Oil|Gas)'
    ]
}

# Tab configurations for multi-tab workflows
RRC_TAB_CONFIGS = {
    "w3a_dual_tab": [
        TabConfig(
            tab_id="gis_viewer",
            tab_type=TabType.GIS_VIEWER,
            url=RRC_URLS["gis_viewer"],
            wait_for_load=True,
            load_timeout=30000,
            required=True
        ),
        TabConfig(
            tab_id="rrc_form",
            tab_type=TabType.PRIMARY_FORM,
            url=RRC_URLS["login"],
            wait_for_load=True,
            load_timeout=30000,
            required=True
        )
    ]
}

# Workflow step configurations
RRC_WORKFLOWS = {
    "w3a_comprehensive": [
        WorkflowStep(
            step_id="setup_tabs",
            name="Setup Dual Tabs",
            description="Initialize GIS viewer and RRC form tabs",
            required_tabs=["gis_viewer", "rrc_form"],
            timeout=60000,
            retry_count=2
        ),
        WorkflowStep(
            step_id="gis_search", 
            name="GIS Well Search",
            description="Search for well location in GIS viewer",
            required_tabs=["gis_viewer"],
            timeout=30000,
            retry_count=2
        ),
        WorkflowStep(
            step_id="rrc_authentication",
            name="RRC Authentication",
            description="Login to RRC system",
            required_tabs=["rrc_form"],
            timeout=30000,
            retry_count=2
        ),
        WorkflowStep(
            step_id="navigate_w3a",
            name="Navigate to W3A",
            description="Navigate to W3A form interface",
            required_tabs=["rrc_form"],
            timeout=45000,
            retry_count=2
        ),
        WorkflowStep(
            step_id="gis_extraction",
            name="GIS Data Extraction", 
            description="Extract location data from GIS system",
            required_tabs=["gis_viewer"],
            timeout=60000,
            retry_count=3
        ),
        WorkflowStep(
            step_id="form_filling",
            name="Form Field Completion",
            description="Fill W3A form fields with extracted and provided data",
            required_tabs=["rrc_form"],
            timeout=90000,
            retry_count=2
        ),
        WorkflowStep(
            step_id="file_attachments",
            name="File Attachments",
            description="Upload required documents (GAU, etc.)",
            required_tabs=["rrc_form"],
            timeout=30000,
            retry_count=2
        ),
        WorkflowStep(
            step_id="area_review",
            name="Area Review Configuration",
            description="Configure area review settings",
            required_tabs=["rrc_form"],
            timeout=45000,
            retry_count=2
        ),
        WorkflowStep(
            step_id="final_submission",
            name="Form Submission/Save",
            description="Submit form or save as draft",
            required_tabs=["rrc_form"],
            timeout=30000,
            retry_count=1  # Don't retry submission
        )
    ]
}

# Default form data
RRC_DEFAULTS = {
    "cementing_company": "BCM & Associates, Inc; PO Box 13077, Odessa, TX 79768; P-5 #040196",
    "contact_title": "Operations Manager",
    "contact_phone": "432-580-7161",
    "contact_email": "operations@bcmandassociates.com",
    "fallback_gis_data": {
        "distance": "4",
        "direction": "northwest",
        "town": "Kermit"
    }
}

# Form configurations by type
RRC_FORM_CONFIGS = {
    "W3A": {
        "name": "Well Plugging Application (W-3A)",
        "selectors": RRC_SELECTORS,
        "workflow": RRC_WORKFLOWS["w3a_comprehensive"],
        "tab_config": RRC_TAB_CONFIGS["w3a_dual_tab"],
        "defaults": RRC_DEFAULTS,
        "supports_multi_tab": True,
        "requires_gis": True,
        "file_attachments": ["GAU_EXAMPLE.pdf"]
    }
}
