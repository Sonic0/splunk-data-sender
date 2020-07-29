URL="https://test.pypi.org/legacy/"

rm -r dist
python3 setup.py sdist bdist_wheel
if twine check dist/*; then
  if [ "$1" = "--test" ] ; then
    twine upload --repository-url ${URL} dist/*
  else
    twine upload dist/*
  fi
fi