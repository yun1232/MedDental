"""Verify the optional calculation environment independently of patient data."""
import io
import numpy as np
import pandas as pd
from PIL import Image

frame = pd.read_csv(io.StringIO('lesion,area_mm2\na,12.5\nb,7.5\n'))
assert float(frame.area_mm2.sum()) == 20.0
assert float(np.mean(frame.area_mm2.to_numpy())) == 10.0
assert pd.read_csv(io.StringIO(frame.to_csv(index=False))).shape == (2, 2)
image = Image.new('RGB', (8, 8), 'white')
output = io.BytesIO()
image.save(output, format='PNG')
output.seek(0)
assert Image.open(output).size == (8, 8)
print('Python, NumPy, Pandas, CSV round-trip and image decoding: passed')
