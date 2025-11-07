# utils/textract_utils.py
import boto3
import os
import config

session_args = {}
if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY:
    session_args["aws_access_key_id"] = config.AWS_ACCESS_KEY_ID
    session_args["aws_secret_access_key"] = config.AWS_SECRET_ACCESS_KEY
    if config.AWS_SESSION_TOKEN:
        session_args["aws_session_token"] = config.AWS_SESSION_TOKEN

region = os.environ.get("AWS_REGION", config.AWS_REGION)
textract_client = boto3.client("textract", region_name=region, **session_args)

# --- helper: call textract detect_document_text (works with S3 object) ---
def extract_text_from_s3(bucket: str, key: str) -> str:
    """
    Uses Textract to get text from the S3 object.
    Returns concatenated text (string).
    """
    try:
        response = textract_client.detect_document_text(
            Document={
                "S3Object": {
                    "Bucket": bucket,
                    "Name": key
                }
            }
        )
        blocks = response.get("Blocks", [])
        texts = []
        for block in blocks:
            if block.get("BlockType") == "LINE":
                texts.append(block.get("Text", ""))
        full_text = "\n".join(texts)
        return full_text
    except (BotoCoreError, ClientError) as e:
        # If textract fails, raise up to caller
        raise RuntimeError(f"Textract failed: {str(e)}")

# --- helper: parse amount and date using regex heuristics ---
AMOUNT_REGEXES = [
    # currency symbol optional, commas optional, decimals optional
    r"([₹₹\$\£]\s?)[\d{1,3},]*(\d+)(?:\.\d{1,2})?",
    r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",  # numbers with commas
    r"amount[:\s]*([0-9]+(?:\.[0-9]{1,2})?)",  # "Amount: 123.45"
    r"total[:\s]*([0-9]+(?:\.[0-9]{1,2})?)",
]

DATE_REGEXES = [
    # common date formats: dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd, mm/dd/yyyy, Jan 1, 2020
    r"(\b[0-3]?\d[\/\-\.][0-1]?\d[\/\-\.](?:\d{2,4})\b)",
    r"(\b(?:\d{4})[\/\-\.](?:0?[1-9]|1[0-2])[\/\-\.](?:[0-3]?\d)\b)",  # yyyy-mm-dd
    r"(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ ,.-]*\d{1,2}[,.-]*[ ,.-]*\d{2,4}\b)",
]

def _find_amount_from_text(text: str):
    # First, look for lines containing 'total' or 'amount' and prefer those
    lines = text.splitlines()
    candidates = []
    for line in lines:
        l = line.strip()
        if not l:
            continue
        if re.search(r"\b(total|amount|grand total|net total|subtotal)\b", l, flags=re.I):
            # search for number in this line
            nums = re.findall(r"[₹\$\£]?\s*[0-9,]+(?:\.[0-9]{1,2})?", l)
            for n in nums:
                cleaned = re.sub(r"[^\d\.]", "", n.replace(",", ""))
                try:
                    candidates.append(float(cleaned))
                except:
                    pass
    if candidates:
        # choose max candidate (often total is the largest)
        return max(candidates)

    # Otherwise, fallback: find any numeric pattern and choose the largest positive number
    all_nums = re.findall(r"[0-9,]+(?:\.[0-9]{1,2})?", text)
    cleaned_nums = []
    for n in all_nums:
        nc = n.replace(",", "")
        try:
            cleaned_nums.append(float(nc))
        except:
            pass
    if cleaned_nums:
        return max(cleaned_nums)

    return None

def _parse_date_string(s: str):
    s = s.strip()
    # try multiple date formats
    fmt_attempts = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"]
    # Normalize separators
    s = s.replace(".", "/").replace("-", "/")
    # Try direct parse
    for fmt in fmt_attempts:
        try:
            dt = datetime.datetime.strptime(s, fmt).date()
            return dt
        except:
            pass
    # Try flexible parsing: remove suffixes like st, nd, rd, th
    s2 = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', s, flags=re.I)
    for fmt in fmt_attempts:
        try:
            dt = datetime.datetime.strptime(s2, fmt).date()
            return dt
        except:
            pass
    return None

def _find_date_from_text(text: str):
    # 1) search line-by-line for common date patterns and parse
    lines = text.splitlines()
    for line in lines:
        for regex in DATE_REGEXES:
            m = re.search(regex, line, flags=re.I)
            if m:
                ds = m.group(1)
                parsed = _parse_date_string(ds)
                if parsed:
                    return parsed

    # 2) fallback: look for words like 'date' followed by something
    m = re.search(r"date[:\s]*([A-Za-z0-9,\/\-\.\s]+)", text, flags=re.I)
    if m:
        candidate = m.group(1).splitlines()[0].strip()
        parsed = _parse_date_string(candidate)
        if parsed:
            return parsed

    return None

def parse_amount_and_date_from_text(text: str):
    """
    Returns (amount: float or None, date: datetime.date or None)
    """
    if not text:
        return None, None

    try:
        amount = _find_amount_from_text(text)
    except Exception:
        amount = None

    try:
        date = _find_date_from_text(text)
    except Exception:
        date = None

    return amount, date
