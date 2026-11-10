# Chore steps-deploy

## Task
Give the deploy steps in order. Files: docs/deploy.md. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

## Files (numbered)
### docs/deploy.md
```
   1	# Deploy checklist
   2	
   3	1. Run the unit tests with `python -m unittest`.
   4	2. Tag the release as `vYYYY.MM.DD`.
   5	3. Build the image with `docker build -t jobq:TAG .`.
   6	4. Push the image to the registry.
   7	5. Roll the workers one at a time and watch the dead count for ten minutes.
   8	6. If the dead count rises, roll back to the previous tag.
```

## Reference facts (what a complete answer states)
F1: 1. Run the unit tests [docs/deploy.md:3]
F2: 2. Tag the release [docs/deploy.md:4]
F3: 3. Build the image with docker build [docs/deploy.md:5]
F4: 4. Push the image to the registry [docs/deploy.md:6]
F5: 5. Roll the workers one at a time [docs/deploy.md:7]
F6: 6. If the dead count rises, roll back to the previous tag [docs/deploy.md:8]

## Not in the files (a reply that says these is wrong)
- regex: `\b(kubectl|helm|terraform|ansible|staging)\b`

## Replies
### R1
```
Here are the deploy steps in order, from `docs/deploy.md`:

1. **Run the unit tests** with `python -m unittest` (line 2).
2. **Tag the release** as `vYYYY.MM.DD` (line 3).
3. **Build the image** with `docker build -t jobq:TAG .` (line 4).
4. **Push the image** to the registry (line 5).
5. **Roll the workers** one at a time and watch the dead count for ten minutes (line 6).
6. **If the dead count rises**, roll back to the previous tag (line 7).
```
### R2
```
- The deploy steps are: 1. Run the unit tests with `python -m unittest` [docs/deploy.md:3]
- The deploy steps are: 2. Tag the release as `vYYYY.MM.DD` [docs/deploy.md:4]
- The deploy steps are: 3. Build the image with `docker build -t jobq:TAG .` [docs/deploy.md:5]
- The deploy steps are: 4. Push the image to the registry [docs/deploy.md:6]
- The deploy steps are: 5. Roll the workers one at a time and watch the dead count for ten minutes [docs/deploy.md:7]
- The deploy steps are: 6. If the dead count rises, roll back to the previous tag [docs/deploy.md:8]
```
### R3
```
- deploy steps run the unit tests with `python -m unittest` [docs/deploy.md:3]
- deploy steps tag the release as `vYYYY.MM.DD` [docs/deploy.md:4]
- deploy steps build the image with `docker build -t jobq:TAG .` [docs/deploy.md:5]
- deploy steps push the image to the registry [docs/deploy.md:6]
- deploy steps roll the workers one at a time and watch the dead count for ten minutes [docs/deploy.md:7]
- deploy steps roll back to the previous tag if the dead count rises [docs/deploy.md:8]
```
### R4
```
- deploy steps runs the unit tests with python -m unittest [docs/deploy.md:3]
- deploy steps tags the release as vYYYY.MM.DD [docs/deploy.md:4]
- deploy steps builds the image with docker build -t jobq:TAG [docs/deploy.md:5]
- deploy steps pushes the image to the registry [docs/deploy.md:6]
- deploy steps rolls the workers one at a time and watches the dead count for ten minutes [docs/deploy.md:7]
- deploy steps rolls back to the previous tag if the dead count rises [docs/deploy.md:8]
```
### R5
```
- deploy steps runs the unit tests with python -m unittest [docs/deploy.md:3]
- deploy steps tags the release as vYYYY.MM.DD [docs/deploy.md:4]
- deploy steps builds the image with docker build -t jobq:TAG [docs/deploy.md:5]
- deploy steps pushes the image to the registry [docs/deploy.md:6]
- deploy steps rolls the workers one at a time and watches the dead count for ten minutes [docs/deploy.md:7]
- deploy steps rolls back to the previous tag if the dead count rises [docs/deploy.md:8]
```
### R6
```
- Run the unit tests with `python -m unittest` [docs/deploy.md:3]
- Tag the release as `vYYYY.MM.DD` [docs/deploy.md:4]
- Build the image with `docker build -t jobq:TAG .` [docs/deploy.md:5]
- Push the image to the registry [docs/deploy.md:6]
- Roll the workers one at a time and watch the dead count for ten minutes [docs/deploy.md:7]
- If the dead count rises, roll back to the previous tag [docs/deploy.md:8]
```
### R7
```
- deploy steps Run the unit tests with `python -m unittest` [docs/deploy.md:3]
- deploy steps Tag the release as `vYYYY.MM.DD` [docs/deploy.md:4]
- deploy steps Build the image with `docker build -t jobq:TAG .` [docs/deploy.md:5]
- deploy steps Push the image to the registry [docs/deploy.md:6]
- deploy steps Roll the workers one at a time and watch the dead count for ten minutes [docs/deploy.md:7]
- deploy steps Roll back to the previous tag if the dead count rises [docs/deploy.md:8]
```
### R8
```
- Run the unit tests with `python -m unittest` [docs/deploy.md:3]
- Tag the release as `vYYYY.MM.DD` [docs/deploy.md:4]
- Build the image with `docker build -t jobq:TAG .` [docs/deploy.md:5]
- Push the image to the registry [docs/deploy.md:6]
- Roll the workers one at a time and watch the dead count for ten minutes [docs/deploy.md:7]
- If the dead count rises, roll back to the previous tag [docs/deploy.md:8]
```
### R9
```
- deploy steps run the unit tests with `python -m unittest` [docs/deploy.md:3]
- deploy steps tag the release as `vYYYY.MM.DD` [docs/deploy.md:4]
- deploy steps build the image with `docker build -t jobq:TAG .` [docs/deploy.md:5]
- deploy steps push the image to the registry [docs/deploy.md:6]
- deploy steps roll the workers one at a time and watch the dead count for ten minutes [docs/deploy.md:7]
- deploy steps roll back to the previous tag if the dead count rises [docs/deploy.md:8]
```
