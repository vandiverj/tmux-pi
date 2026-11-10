# Deploy checklist

1. Run the unit tests with `python -m unittest`.
2. Tag the release as `vYYYY.MM.DD`.
3. Build the image with `docker build -t jobq:TAG .`.
4. Push the image to the registry.
5. Roll the workers one at a time and watch the dead count for ten minutes.
6. If the dead count rises, roll back to the previous tag.
