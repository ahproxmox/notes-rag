import os

# Never push indexing metrics to the real Pushgateway from tests.
os.environ['PUSHGATEWAY_URL'] = ''
